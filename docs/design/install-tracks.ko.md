# 설치 트랙: 개발자와 운영자

[English](install-tracks.md) | 한국어

상태: 설계 합의 (2026-10-18). R1 구현·배포 완료(13절). 10절의 각 단계는 시작하기 전에 확인을 받는다. 관련 문서:
[mesh-join.ko.md](mesh-join.ko.md), [headscale 운영](../operations/headscale.ko.md),
[backup-restore.ko.md](../operations/backup-restore.ko.md).

## 1. 문제

지금은 모든 설치와 업데이트가 관리용 Mac에서 시작한다. `scripts/install-agent-service.sh`(549줄)가 Go 에이전트,
goindex, 대시보드를 Mac에서 빌드해 SSH로 올리고, 허브에서 할 단계를 모두 원격으로 실행한다. Mac은 허브의 fleet 키도
발급하고(`setup-hub-ssh.sh`), LLM 토큰을 배포하며(`deploy-llm-env.sh`), 이름 가리기 규칙을 보관하고 공개 사본을 만든다.

그래서 이 일들 대부분이 개발이 아닌데도, Server Guardian을 운영하려면 개발용 기기가 있어야 한다. 그리고 Mac에는 서로
다른 두 가지, 작업용 기기와 허브 밖의 비상용 자격 증명이 섞여 있다.

## 2. 목표

지금 섞여 있는 세 역할을 나눈다.

| 역할 | 무엇인가 | 필요한 것 |
|---|---|---|
| **개발자 트랙** | 코드를 바꾸고, CI를 돌리고, 릴리스를 빌드·서명하고, 공개 사본을 만든다 | 저장소와 도구 체인이 있는 macOS 또는 Linux |
| **운영자 트랙** | Server Guardian을 설치, 업데이트, 운영한다 | 평소에는 브라우저, 첫 설치 때만 허브의 셸 |
| **비상용 자격 증명** | 허브나 tailnet이 죽었을 때 노드에 닿는다 | 허브 밖에 보관한 암호화 번들과 클라우드 콘솔. 기기가 아니다 |

운영자는 아무것도 빌드하지 않고, 상시 관리 PC도 필요 없다. 허브 밖에 반드시 있어야 하는 것은 비상용 자격 증명뿐이다.

## 3. 무엇이 어디로 가는가

| 지금 (관리용 Mac) | 개발자 트랙 | 운영자 트랙 |
|---|---|---|
| `scripts/ci.sh` | 그대로 | — |
| `install-agent-service.sh` (빌드 + 원격 설치) | 빠른 개발 배포용으로 남긴다. 트리를 올리고 허브 쪽 설치 스크립트를 실행하도록 고친다 (R1) | — |
| — | `scripts/release.sh`: 빌드, 서명, 게시 (R2) | — |
| — | — | 검증한 릴리스로 허브에서 실행하는 `install.sh` (R3) |
| 바뀔 때마다 재배포 | — | 대시보드 업데이트와 자동 되돌리기 (R4) |
| `setup-hub-ssh.sh` (fleet 키 발급·등록) | 그대로 | 첫 노드: 노드 등록 화면의 키 줄을 노드에 붙여 넣는다(cloud-init, 콘솔). 키 교체는 허브에서 (R5) |
| `deploy-llm-env.sh`, `deploy-llm-skills.sh` | 그대로 | 허브에서 토큰을 읽어 fleet SSH로 Tier-1 노드에 보내는 허브 쪽 버전 (R5) |
| `export-public.sh`, `redact-private.py` | 그대로 | — |
| `export-credentials.sh` | 그대로 (개발자 자신의 번들) | 설치 때 한 번 만드는 비상용 번들 (R3) |
| 노드 추가 | — | 대시보드의 메시 가입 승인과 노드 등록 (완료) |

## 4. 릴리스

### 4.1 내용물

아키텍처마다 압축 파일 하나, `server-guardian-<version>-linux-<arm64|amd64>.tar.gz`:

- `bin/agent`, `bin/goindex`: `CGO_ENABLED=0`으로 빌드하고 `AgentVersion`에 `<version>`을 넣는다.
- `ui/`: 빌드한 대시보드(`apps/web-desktop/dist`).
- `cli/`, `configs/`, `deploy/`, `tools/`, 허브 쪽 `install.sh`: `git archive <tag>`에서 가져온다(`.git`, 테스트,
  운영자 안내서를 뺀 문서는 넣지 않는다).
- `MANIFEST`: 모든 파일의 sha256. `VERSION`.

옆에 `<archive>.sha256`과 `<archive>.sig`를 둔다. 서명은 압축 파일 전체에 건다.

허브는 빌드하지 않는다. 허브에는 npm이 없고 들여서도 안 된다(npm install 스크립트가 코드를 실행한다). 허브에서 빌드하면
도구 체인이 보안 경계 안으로 들어온다.

### 4.2 버전

git 태그 `vYYYY.MM.DD`(같은 날 두 번째 릴리스는 `.2`). `AgentVersion`과 `/api/status`가 이 값을 알린다. 허브는 설치된
것보다 오래된 버전을 거부한다. 기록된 이전 버전으로 되돌리는 경우만 예외다.

### 4.3 서명

허브는 설치 때 `/etc/guardian/release_signers`(root, 0644)에 고정한 공개키로 모든 릴리스를 검증한다. 에이전트는 이
파일을 바꿀 수 없다(`/etc/guardian`은 이미 유닛의 `InaccessiblePaths`에 있다). 그래서 에이전트가 뚫려도 개발자가 서명한
다른 릴리스를 요청하는 것이 고작이다.

선택지:

| | `ssh-keygen -Y sign` / `-Y verify` | minisign |
|---|---|---|
| 도구 | OpenSSH 8.1+, Mac과 허브에 이미 있음 | 양쪽에 패키지를 더 설치 |
| 키 | ed25519 또는 하드웨어 키(`ed25519-sk`) | ed25519 |
| 형식 | 네임스페이스(`server-guardian-release`)가 있는 `allowed_signers` | `.minisig` |

권장: `ssh-keygen -Y`. 새로 설치할 것이 없고 하드웨어 키도 쓸 수 있다. 서명 키는 개발자 기기와 개발자 자신의 암호화
번들에만 두고, 허브에는 절대 두지 않는다.

### 4.4 릴리스를 올릴 곳

무결성은 서명이 지키므로, 올릴 곳은 늘 받을 수 있기만 하면 된다.

| | Gitea (git.example.com) | 공개 사본의 GitHub Releases | 둘 다 |
|---|---|---|---|
| 접근 | 저장소가 비공개라 허브에 읽기 토큰이 필요하다(허브에 새 비밀) | 공개, 토큰 없음 | 허브는 공개 쪽을 쓴다 |
| 내용 | main으로 빌드 | main으로 빌드. 코드와 설정에는 placeholder만 있고, `redact-private.py --check`를 통과해야 한다 | — |
| 장애 | 허브와 함께 멈춘다(Gitea가 허브에서 돈다) | 플릿과 무관하다 | — |

권장: 공개 사본의 GitHub Releases. 허브에 비밀이 생기지 않고, 허브(와 Gitea)가 죽어도 받을 수 있다. 외부에 공개하는
일이므로 사용자가 결정한다.

## 5. 허브 배치

두 트랙이 같은 배치를 쓴다. 그래서 개발자 배포와 운영자 업데이트가 허브를 같은 모양으로 남긴다.

```text
~/.guardian/
  releases/<version>/      풀어 놓은 릴리스, 읽기 전용 (개발자 배포는 <git describe>-dev)
  current -> releases/<v>  유닛이 실행하는 것
  previous -> releases/<v> 되돌릴 대상
  agent.env                그대로
  data/                    그대로 (운영 상태)
```

- 에이전트 유닛은 `~/.guardian/current/bin/agent`를 `WorkingDirectory=~/.guardian/current`,
  `GUARDIAN_CLI=~/.guardian/current/cli/llm-agent/guardian.py`로 실행한다. 타이머도 같은 경로를 쓴다.
- `~/server-guardian`(지금의 checkout)은 더 이상 실행되지 않는다. 개발자 트랙에서만, 내장 `server-guardian` 코드
  프로젝트와 그 작업 클론(`GUARDIAN_SELF_REPO`)의 원본으로 남는다. 운영자 설치에는 자기 자신 프로젝트가 없다.
- 릴리스는 3개(current, previous, 하나 더)까지 남긴다.

## 6. 허브 쪽 설치 스크립트 (`install.sh`)

지금 원격 설치가 하는 단계를, sudo가 있는 로그인 사용자로 허브에서 실행한다. 첫 설치, 개발자 배포, 업데이트가 같은
스크립트를 쓴다.

1. 플랫폼(Ubuntu 또는 Debian, systemd, arm64/amd64)과 준비물(python3, git, openssl, tailnet에 가입한 tailscale)을
   확인한다.
2. `releases/<version>/`에 풀고 `MANIFEST`를 확인한다.
3. 첫 설치만: `~/.guardian/data`를 만들고, 허브에서 `AGENT_TOKEN`을 만들고, `/etc/guardian/release_signers`를
   고정하고, inventory에 허브 항목 하나(id, hostname, `tailscale ip -4`로 찾은 Tailscale IP, `guardian_role: hub`)를
   넣는다.
4. fleet 키 보호(`deploy/hub/protect-fleet-key.sh`), systemd 유닛, sudo drop-in, sshd 하드닝, 백업과 야간 점검 타이머:
   지금과 같다.
5. 선택 구성 요소는 첫 설치 때 묻고 `agent.env`에 기억한다: 판정자 패치용 Go toolchain(Go 코드 프로젝트에만 필요),
   LLM CLI(`install-llm-clis.sh`의 로직).
6. `current`를 바꾸고, 재시작하고, 상태를 확인하고(7.3절), 대시보드 주소와 토큰 위치를 출력한다.
7. 첫 설치만: 비상용 자격 증명 절차를 출력한다(9절).

`install-agent-service.sh`는 빌드하고, 같은 트리를 만들어 올리고, `install.sh --dev`를 실행하는 스크립트가 된다. Mac
전용 로직(크로스 컴파일, sha256 확인과 Tailscale 재시도가 있는 업로드)은 그대로 두고, 허브를 바꾸는 일은 모두
`install.sh`로 옮긴다.

## 7. 대시보드에서 업데이트

### 7.1 누가 무엇을 하나

에이전트는 `NoNewPrivileges`로 돌고, 자기 자신을 바꿀 권한을 받아서는 안 된다. 그래서 업데이트를 나눈다.

- **에이전트**(권한 없음): 설정 > 업데이트에 설치된 버전, 최신 릴리스(허브가 릴리스 목록을 읽음)와 그 설명을 보여 준다.
  "설치"를 누르면 `~/.guardian/data/update/request.json`(`{version, requested_by, at}`)과 audit 줄을 쓴다.
- **업데이터**(그 파일을 지켜보는 `server-guardian-updater.path`가 `server-guardian-updater.service`를 시작한다. 허브
  쪽 설치 스크립트를 필요한 권한으로 실행하는 oneshot이다): 업데이트를 하고 `update/result.json`과 로그를 쓴다. 대시보드가
  그것을 보여 준다.

### 7.2 업데이터의 단계

1. 토론이나 액션이 실행 중이면 거부한다(배포 때와 같은 확인). 다시 시도는 운영자가 한다.
2. 압축 파일, `.sha256`, `.sig`를 임시 디렉터리로 받는다. 서명을 `/etc/guardian/release_signers`로, sha256을 확인하고,
   맞지 않으면 거부한다.
3. `current`보다 오래된 버전은 거부한다. `previous`(되돌리기)만 예외다.
4. DB를 백업한다(`db_backup.py`, 야간 타이머와 같다).
5. 새 릴리스의 `install.sh --update`를 실행한다.
6. 상태를 확인한다. 실패하면 `current`를 이전 릴리스로 되돌리고, 재시작하고, 실패를 기록한다.

### 7.3 상태 확인

재시작 뒤 60초 안에 `/api/status`가 답하고 새 버전을 알려야 하며, 새 트리에서 `guardian.py --help`가 실행되어야 한다
(`--version` 플래그는 R4에서 더한다). 확인에 실패하면 자동으로 되돌린다.

### 7.4 정책

기본은 수동이다. 대시보드가 업데이트가 있다고 알리고, 사람이 설치한다. 야간 자동 업데이트는 나중의 선택지다
(`GUARDIAN_AUTO_UPDATE`, 기본 꺼짐).

## 8. 관리 PC 없이 하는 평소 운영

- 노드는 메시 가입 승인으로 들어오고 노드 등록으로 추가된다(완료). 허브 키가 아직 없는 노드는 등록 화면이 정확한
  `authorized_keys` 줄을 보여 주고, 운영자가 클라우드 콘솔이나 cloud-init으로 그 줄을 넣는다.
- LLM 토큰: 허브에서 입력하고(`~/.guardian/llm.env`) 허브 쪽 `deploy-llm-env`가 Tier-1 노드로 보낸다 (R5).
- fleet 키 교체는 허브에서 한다. 허브는 이미 옛 키로 모든 노드에 닿는다 (R5).
- 토론, 액션, 야간 점검, 백업: 지금과 같다.

## 9. 비상용 자격 증명

허브나 tailnet이 죽었을 때, 허브를 복원하거나 headscale을 고치거나 노드에 닿으려면 필요하다.

- **최소:** 클라우드 공급자의 콘솔(시리얼 또는 인스턴스 콘솔)과 계정 복구. 보관할 것이 없지만, 느리고 공급자마다 다르다.
- **권장:** 허브 밖에서 만든 SSH 키 쌍(아무 기기에서나 `ssh-keygen`). 공개키는 허브의 `from=` 제한 없이 모든 노드의
  `authorized_keys`에 넣고, 개인키는 오프라인에 둔 암호화 번들에만 둔다(두 번째 장소에 사본 하나). 허브는 개인키를 보지
  못한다.
- 번들에는 다음도 넣는다:
  - headscale 백업(headscale 백업을 제어 서버 밖으로 보내기 전까지);
  - 개발자 트랙의 릴리스 서명 키;
  - `llm.env`, 이름 가리기 규칙, `CLAUDE.local.md`(지금 번들의 빈 곳).

첫 설치는 이 절차를 출력하고, 운영자가 번들이 있다고 확인할 때까지 점검 목록을 끝내지 않는다. 쓰는 방법은
[headscale 운영](../operations/headscale.ko.md)의 "비상 접속"과 [backup-restore.ko.md](../operations/backup-restore.ko.md)에
있다.

## 10. 단계

| 단계 | 내용 | 지금 허브에 생기는 변화 |
|---|---|---|
| **R1** | 허브 쪽 `install.sh`; `current`/`previous`가 있는 릴리스 디렉터리 배치; `install-agent-service.sh`가 트리를 올리고 이를 호출 | 허브가 새 배치로 옮겨 간다. 동작은 같다 |
| **R2** | `scripts/release.sh`(두 아키텍처 빌드, MANIFEST, 서명, 게시); 첫 태그 | — |
| **R3** | 검증한 릴리스로 운영자 첫 설치; 운영자 안내서(준비물: headscale 템플릿, DNS, 첫 노드, 비상용 번들) | — |
| **R4** | 업데이터 path/service, 설정 > 업데이트, 되돌리기 | Mac 없이 업데이트 |
| **R5** | 허브 쪽 LLM 토큰 배포와 fleet 키 교체; 번들의 빈 곳 | 운영에 Mac이 더 이상 필요 없다 |

각 단계는 평소의 관문을 거쳐 배포하고, 개발자 배포가 계속 동작하게 한다.

## 11. 결정 사항 (2026-10-18)

1. 서명: 네임스페이스 `server-guardian-release`의 `ssh-keygen -Y`(하드웨어 키는 써도 되고, 필수는 아니다).
2. 릴리스 위치: 공개 사본(`owner/server-guardian-public`)의 GitHub Releases. 허브는 토큰 없이 그곳에서 받는다.
3. 버전: 날짜 태그 `vYYYY.MM.DD`(같은 날 두 번째 릴리스는 `.2`).
4. 비상용: 허브 밖에서 만든 별도 SSH 키. 공개키는 `from=` 없이 모든 노드에, 개인키는 오프라인 암호화 번들에만 둔다.
5. 운영자 트랙은 ops와 코드 프로젝트를 모두 지원한다. Go toolchain은 설치 선택지로 남긴다.

## 12. 위험

- **서명 키를 도난당하면** 모든 허브가 설치하는 릴리스를 누군가 내보낼 수 있다. 하드웨어 키나 암호화 번들에만 두고,
  교체는 옛 키로 서명한 릴리스에 새 키를 `release_signers`로 넣는 방식으로 한다.
- **업데이터는 에이전트보다 권한이 많다.** 입력은 버전 문자열뿐이고, 설치하는 것은 모두 서명 검사를 통과해야 하며, 요청에
  담긴 코드는 실행하지 않는다.
- **배치 이전(R1)은 운영 중인 허브를 건드린다.** 먼저 평가 샌드박스(`~/guardian-eval`)에서 dry run을 하고,
  `~/server-guardian`으로 돌아가는 방법을 시험해 두어야 한다.
- **두 설치 경로가 서로 어긋날 수 있다.** 두 트랙 모두 같은 `install.sh`를 쓰고, CI가 그것을 임시 디렉터리에서
  실행한다.

## 13. 구현 메모

### R1 (2026-10-18)

구현: `deploy/hub/install.sh`(옆에 `install_go.py`, `isolation-check.sh`, `agent.env.examples`),
`~/.guardian/current`를 실행하는 유닛, 빌드·업로드 뒤 이를 호출하도록 다시 쓴 `scripts/install-agent-service.sh`.

- **릴리스 이름**은 `<git describe --dirty>-<UTC 시각>`이다. 같은 커밋을 다시 배포해도 새 디렉터리가 생긴다.
  `AgentVersion`은 `git describe` 값 그대로이고, 재시작 뒤 `install.sh --expect-version`이 비교한다.
- **릴리스 트리**에는 작업 트리에 있는 그대로의 `cli/`, `configs/`, `deploy/` 추적 파일(테스트와 `testenv.py` 제외,
  추적하지 않는 파일은 절대 넣지 않음)과 `bin/agent`, `bin/goindex`, `ui/`, `VERSION`, `MANIFEST`(525개 파일)가 들어간다.
  확인한 뒤 읽기 전용으로 바꾼다. `__pycache__`는 생기지 않는다.
- **시작 실패** 때는 `~/.guardian/units.prev/`에 저장한 유닛 파일과 이전 링크를 되살려 그것을 시작한다(exit 3). 첫
  이전에는 이전 릴리스가 없으므로, 옛 유닛이 옛 배치(`~/.guardian/agent`, `~/server-guardian`)를 되살린다. 이를 위해 옛
  배치는 그대로 남겨 두었다.
- **실행 중인 토론:** `install.sh`(설치와 `--rollback`)는 `guardian.py --result-file` 프로세스가 돌고 있으면 exit 4로
  거부한다. `--force`로 넘길 수 있다. 아래 사고 뒤에 추가했다.
- **goindex**는 `~/.local/libexec/guardian/goindex -> ~/.guardian/current/bin/goindex` 링크라서 되돌리기를 따라간다.
  skill `guardian-fleet-ops`는 이제 `~/.guardian/current/cli/llm-agent`에서 작업한다.
- **허브에서 확인한 것:** dry run(바뀐 것은 유닛 세 개의 경로뿐이고, 운영자의 `agent.env` 키는 유지), 이전, 읽기 전용
  릴리스에서의 API와 CLI 호출(프로젝트, 팀, 토론, 야간 점검 dry run), `--rollback`으로 되돌렸다가 다시 앞으로.
- **하지 않은 것:** CI는 `install.sh`를 실행하지 않는다(systemd와 sudo가 필요). `bash -n`과 shellcheck로만 검사한다.
  옛 파일 `~/.guardian/agent`, `agent.prev`, `ui/`는 옛 배치가 되돌릴 곳으로 필요 없어질 때까지 남긴다.

**사고.** 되돌리기 시험 중 두 번째 배포가, 대시보드 토론(`dbt_20261018_050641_0640b0`, fleet, 4라운드)이 실행 중인데
허브 에이전트를 재시작했고, 그 토론은 `interrupted`로 끝났다. 배포 전 확인이 그 토론을 보여 줬지만, 명령이 `&&`가 아닌 `;`로
이어져 있어 멈추지 않았다. 같은 과제, 팀, 스레드로 토론을 다시 시작했고(`dbt_20261018_050914_5721b9`), 이제
`install.sh`가 스스로 거부한다.

# Fleet 프로비저닝: 계획에 따른 패키지 설치와 삭제

[English](fleet-provisioning.md) | 한국어

상태: 제안됨 (2026-10-19, 범위 결정은 같은 날 운영자가 내림, 2장); P1-P6는 구현해 2026-10-20 `6b24987`로 배포했다. 관련 문서:
[server-ops-project.ko.md](server-ops-project.ko.md) (ops 프로젝트, 이 문서가 확장하는 액션 카탈로그),
[multi-perspective.ko.md](multi-perspective.ko.md), [mesh-join.ko.md](mesh-join.ko.md).

## 1. 문제

운영자는 LLM에 명령을 내리면("웹 노드에 nginx를 설치해", "빌드 노드의 오래된 Node를 지워") 그 명령을 각 노드의
실제 상태에 맞춰 해석하고, 설치 계획을 세운 뒤, 그 계획대로 실행하게 하려 한다. `fleet` 프로젝트에서는 이것이
동작하지 않는다. 코드와 토론 `thread_20261019_025735_911ea1`에서 찾은 원인:

| # | 막히는 곳 | 근거 |
|---|---|---|
| 1 | 설치와 삭제 액션이 없다. | `ops_actions.ACTIONS`에는 정리 액션 7개뿐이다. 나머지는 `jev_mcp:server_remediation`(자유 셸 한 줄, 위험 high, preview와 verify 없음)인데 판정자가 거부한다. |
| 2 | 토론 에이전트가 계획에 필요한 것을 볼 수 없다. | 점검 10개는 Linux와 디스크 중심이다(`df -x`, `free`, `journalctl`, apt 캐시). OS, 패키지 매니저, 설치된 패키지, sudo, 노드에서 무엇이 도는지 알려 주는 점검이 없다. 인벤토리에는 `arch`뿐이다. macOS 노드에서는 점검 대부분이 실패했다. |
| 3 | 단위가 "노드 1대에 액션 1개"다. | 노드별 단계, 단계 간 의존성, 중단 규칙이 없고 승인 화면도 액션 단위로만 동작한다. |
| 4 | 팀이 정리용으로 만들어져 있다. | `ops_sre` 프롬프트는 "가장 덜 파괴적인 것부터, 카탈로그 정리 액션을 우선"이다. 맞는 액션이 없으면 판정자는 "자동 액션 없음, 운영자가 수동으로"로 끝낸다. |
| 5 | 자동 경로가 의도적으로 닫혀 있다. | `policy.auto_run`은 low 위험 카탈로그 액션만 받는다. 설치는 최소 medium이다. |

`server-ops-project.ko.md` 2장은 "구성 관리 도구를 대체하지 않는다"를 비목표로 적고 있다. 이 문서가 그 장을
수정한다(2장 결정 1).

## 2. 결정 (운영자, 2026-10-19)

1. **범위.** fleet 노드의 패키지 설치와 삭제는 범위에 든다. `server-ops-project.ko.md`의 비목표는
   "일반적인 구성 관리는 하지 않는다: 파일, 템플릿, 사용자, 서비스 설정은 관리하지 않고 패키지는 관리한다"로
   바뀐다.
2. **보호.** 설치는 허용 목록으로 제한하지 않는다. 삭제와 부작용만 **보호 목록**으로 막는다: 풀 수 없는 내장
   목록, 프로젝트가 더하는 항목, **노드의 역할**에 따른 보호(6장).
3. **패키지 도구.** apt와 brew 외에 다른 Linux 매니저와 언어 수준 도구(npm, bun 등)를 지원한다. 단계가 붙은 목록은
   4장이다.
4. **sudo.** 노드가 비밀번호 없이 허용하는 곳에서만(`sudo -n`) 쓴다. 허용하지 않는 노드는 바꾸지 않고, 계획이
   이유를 보고한다(5.4).

유지되는 기존 결정: Python 엔진은 표준 라이브러리만 쓴다. fleet 노드에는 아무것도 설치하지 않는다(허브가 SSH로
닿는다). 설치용 자유 셸 한 줄은 추가하지 않는다. 사람이 승인한다(패키지에는 `auto_run` 없음).

## 3. 한눈에 보는 설계

```
명령(텍스트)
  -> Expert가 노드 사실과 패키지 정보를 읽는다 (읽기 전용 점검)      5장
  -> Expert가 계획을 쓴다: 노드별 단계, 순서, 중단 규칙             7장
  -> 검증기 (코드, 모델 없음): 범위, 매니저, sudo, 보호 목록           6장
  -> 시뮬레이션으로 preview를 만들어 계획과 함께 저장
  -> 사람이 계획을 승인한다 (한 화면에 모든 단계)
  -> 실행기: 노드별 순서대로 drift 검사, 실행, verify               8장
```

## 4. 패키지 도구

"단계"는 10장의 구현 단계다. 모든 도구는 같은 다섯 함수(`facts`, `info`, `simulate`, `argv`, `query`)를 가진
어댑터이며, 어떤 어댑터도 셸 문자열을 조립하지 않는다.

| 도구 | 대상 | 관리하는 것 | 권한 | preview | 단계 |
|---|---|---|---|---|---|
| apt (`apt-get`, `dpkg`) | Debian, Ubuntu | 시스템 패키지 | `sudo -n` | 시뮬레이션 (`apt-get -s`) | P2 |
| dnf (`yum`은 별칭) | RHEL, Fedora, Rocky, Alma, Amazon Linux | 시스템 패키지 | `sudo -n` | 시뮬레이션 (`--assumeno`) | P2 |
| brew | macOS, Linuxbrew | formula, cask | 사용자, root 불가 | 설치는 시뮬레이션 (`--dry-run`), 삭제는 의존 패키지 목록 (`brew uses --installed`) | P2 |
| npm (global) | Node가 있는 곳 | 전역 CLI 패키지 | 사용자, prefix가 root 소유면 `sudo -n` | 시뮬레이션 (`--dry-run`) | P2 |
| bun (global) | Bun이 있는 곳 | 전역 CLI 패키지 | 사용자 | 목록만 (dry run 없음) | P2 |
| zypper | SUSE | 시스템 패키지 | `sudo -n` | 시뮬레이션 (`--dry-run`) | P2b |
| apk | Alpine | 시스템 패키지 | root 또는 `sudo -n` | 시뮬레이션 (`--simulate`) | P2b |
| pacman | Arch | 시스템 패키지 | `sudo -n` | 출력 (`-Sp`, `-Rp`) | P2b |
| pnpm, yarn (global) | Node가 있는 곳 | 전역 CLI 패키지 | 사용자 | 목록만 | P2b |
| pipx, uv tool | Python이 있는 곳 | Python 명령줄 앱 (시스템 Python에 `pip`은 안 함: PEP 668) | 사용자 | 목록만 | P2b |
| snap | Ubuntu | snap | `sudo -n` | 목록만 | 나중 |
| flatpak | 데스크톱 Linux | 앱 | 사용자 또는 `sudo -n` | 목록만 | 나중 |
| nix (`nix profile`) | Nix가 있는 곳 | 프로필 패키지 | 사용자 | 시뮬레이션 | 나중 |
| cargo install, go install, gem, composer global | 툴체인이 있는 곳 | 소스 빌드 또는 언어별 | 사용자 | 목록만 | 나중 (노드에서 코드를 컴파일함) |
| mas, winget, choco, scoop | macOS App Store, Windows | - | - | - | 계획 없음 (GUI 로그인이 필요하거나 fleet에 Windows가 없음) |
| docker 이미지, compose | Docker가 있는 곳 | 서비스이며 패키지가 아님 | - | - | 범위 밖 (별도의 서비스 배포 설계) |

"목록만"은 preview가 시뮬레이션이 아니라 현재 상태와 정확한 명령을 보여 준다는 뜻이다. 이런 단계는 표시되고,
`auto_run`이 가져갈 수 있는 계획에 들어가지 않으며, 단계의 위험을 한 단계 올린다.

(운영자가 "burn"이라고 적었고 **bun**으로 읽었다. 다른 것을 뜻했다면 이 장을 고친다.)

## 5. 노드 보기: 사실과 패키지 정보

### 5.1 새 읽기 전용 점검 (`ops_checks.py`)

| 점검 | 인자 | 결과 |
|---|---|---|
| `host_facts` | `host` | `os_family`(linux, macos), `os_id`, `os_version`, `arch`, `init`(systemd, launchd, 기타), 발견된 패키지 매니저(`command -v`), `sudo_nopasswd`(`sudo -n true`), 로그인한 사용자, 활성화되었거나 실행 중인 서비스와 각각을 소유한 패키지(`dpkg -S`, `rpm -qf`, `brew services list`), 열린 포트와 소유 프로세스 및 패키지 |
| `package_info` | `host`, `manager`, `name` | 매니저 색인에 이름이 있는지, 후보 버전, 설치된 버전, 의존성, 삭제라면 그것에 의존하는 패키지 |

둘 다 다른 점검처럼 감사된다(`source=ops_check`). 출력은 데이터이며 지시가 아니다(`server-ops-project.ko.md` 5.2와
같은 규칙). 기존 점검에는 macOS 변형이 붙는다(`df -h -I`, `ps -Ao pid,pcpu,rss,comm -r`, `vm_stat`,
`launchctl list`, `tmutil listlocalsnapshots /`). `uptime_load`는 알 수 없는 노드에서 Linux 마커 파일을 "재부팅
필요"의 근거로 쓰지 않는다.

### 5.2 사실 캐시

`host_facts` 결과는 시각과 함께 `<data dir>/ops-facts/<host>.json`에 저장된다. 검증기(6장)는 15분 이내의 사실만
쓰고, 그보다 오래되면 점검을 다시 실행한다. 캐시는 운영 상태이므로 테스트는 `testenv.isolate_data_dir()`를 쓴다.

### 5.3 인벤토리

인벤토리 항목에 `ops_roles`(역할 이름 목록, 6.2)를 둘 수 있다. `servers.json`은 계속 로컬 스냅샷이고,
`servers.example.json`이 이 필드를 보여 준다.

### 5.4 sudo

시스템 매니저 단계에는 `sudo_nopasswd: true`이거나 root 로그인이어야 한다. 아니면 검증기가 그 단계를
`INVALID: sudo_required`로 표시하고, 그 노드에 대한 계획은 승인할 수 없으며 시도하지 않는다. 같은 계획의 다른
노드는 영향받지 않는다. 사용자 수준 도구(brew, bun, pipx)는 sudo 없이 돌고, npm은 prefix가 root 소유이고 사실이
sudo 가능을 보일 때만 `sudo -n`을 쓴다.

## 6. 보호

삭제나 설치가 보호 패키지를 직접 지우거나, 시뮬레이션에 나타난 부작용(autoremove, 의존성, 충돌)으로 지우게 되면
거부된다(`INVALID`, 패키지 이름을 밝힘).

### 6.1 내장 목록 (프로젝트가 풀 수 없음)

매니저 계열별로, 이름이나 glob으로 맞추며 `ops_packages.py`에서 읽는다:

- 노드에 닿고 제어하는 것: ssh 서버와 클라이언트, `sudo`, `tailscale*`, `headscale*`, `systemd*`, `dbus*`,
  macOS의 `launchd`가 소유한 시스템 패키지, 쓰고 있는 네트워크 매니저.
- 실행 중인 커널과 모듈, 부트로더, `libc*`, `glibc`, 패키지 매니저 자신과 그 라이브러리(`apt`, `dpkg`, `rpm`,
  `dnf`, `brew`), init 시스템.
- Guardian 자신이 닿는 노드에서 필요한 것: 점검 명령이 쓰는 인터프리터(`coreutils`, `findutils`, `procps`,
  `bash`/`zsh`, `apt`가 의존하는 Debian 계열의 `python3`).
- 다른 관리 도구 뒤에 있는 언어 런타임: 보호되는 전역 npm 패키지가 설치되어 있는 동안 `nodejs` 삭제는 거부된다.

### 6.2 역할에 따른 보호

보호는 노드가 하는 일을 따르며, 두 출처를 합친다:

1. **선언된 역할.** 인벤토리 항목의 `ops_roles`(예: `web`, `database`, `gitea`, `headscale`, `llm_cli_host`,
   `build`, `monitoring`)가 `configs/package-roles.json`을 거쳐 매니저별 보호 패키지 패턴이 된다. 예: 역할
   `llm_cli_host`는 허브의 토론 에이전트가 쓰는 Node 런타임과 전역 CLI 패키지를 보호하고, 역할 `gitea`는 `gitea`와
   그 데이터베이스 클라이언트를 보호한다. 이 파일은 저장소의 일부이며 `scripts/ci.sh config`가 검증한다.
2. **실행 중인 것에서 도출.** `host_facts`에서: 활성화되었거나 실행 중인 서비스를 소유한 패키지와, 열린 포트를 가진
   프로세스를 소유한 패키지는 그 노드에서 보호된다. 한 시간 전에 설치해 시작한 패키지도 도는 순간부터 보호된다.

프로젝트는 `policy.protected_packages`로 더 추가한다(`deny_paths`처럼 추가만 가능).

계획의 보호 목록은 검증기가 한 번 계산해 노드별로 승인 화면에 보여 주고 계획과 함께 기록한다. 그래서 나중에
역할이 바뀌어도 승인된 내용은 바뀌지 않는다.

### 6.3 설치 번들

역할(6.2)은 거부권이다. 무엇을 지우면 안 되는지만 말하고 아무것도 설치하지 않는다. **번들**은 그 반대이고 별개이다.
운영자나 판정자가 설치를 *제안*할 수 있는, 매니저별 정확한 패키지 목록에 이름을 붙인 것이다. 파일을 둘로 나눈 이유는 두
목록이 원하는 모양이 반대이기 때문이다(거부권은 넓을수록, 제안은 정확할수록 안전하다). 또 설치까지 하는 역할은 "더 보호"를
"더 설치"로 바꿔 버린다.

`configs/package-bundles.json`: `{번들: {description, reviewed, notes, packages: {매니저: {required, optional}}}}`.

- `required`는 없으면 번들이 동작하지 않는 것, `optional`은 이름으로 요청하지 않으면 빼는 부가 패키지이다. 그래서 일부만
  설치하는 것이 기본이고, "역할을 가졌다"는 이유로 설치되는 것은 없다.
- 필수 패키지는 빼도 되지만(`skip`) 전개 결과가 그 사실을 보고하고, 선택 패키지는 이름으로 더한다. 번들에 없는 이름은 거부한다.
- 번들은 권한이 아니다. `package_bundles.expand(name, manager, optional, skip, installed)`는 `ops:package_install`에 줄 평범한
  이름을 돌려주고 노드에 이미 있는 것은 뺀다. 각 이름은 직접 쓴 패키지와 똑같이 검증기, 시뮬레이션, 보호 목록, 드리프트
  검사를 거치며, 패키지별(또는 묶음별) 계획 단계가 단계 단위 승인을 유지한다.
- `notes`는 승인 화면과 판정자가 놓치면 안 되는 한 줄 사실이다(예: `dnf`는 `postgresql-setup --initdb`가 필요한데
  패키지 액션은 그것을 하지 않는다).
- **버전은 배포판이 정하고, 번들은 이름에 버전이 들어가는 곳에서만 고정한다**(2026-10-19 결정). "LTS"는 하나의 개념이
  아니다. PostgreSQL에는 LTS가 없고(메이저 버전을 약 5년 지원한다), nginx에는 stable/mainline 브랜치가 있으며, Node.js와
  MariaDB에는 LTS 계열이 있다. apt와 dnf의 패키지 이름에는 버전이 없어서 버전은 노드 OS 릴리스가 싣고 있는 것이 되므로,
  OS를 LTS 릴리스로 유지하면 서비스도 거기에 머문다. 설치 시뮬레이션은 이미 승인 화면에 설치될 후보 버전을 보여 준다.
  이름 자체에 메이저 버전이 들어가는 곳(brew `postgresql@17`)에서는 번들이 곧 정책이다. 패키지 액션은 벤더 저장소(PGDG,
  nginx.org)를 절대 추가하지 않는다. 검증된 경로 밖에 서명 키와 소스를 더하게 되기 때문이다. 진짜 LTS 계열이 있는 서비스는
  번들 이름에 계열을 넣고(예: `nodejs_lts`) 어느 계열인지 노트 한 줄을 단다.
- `reviewed`(필수, `YYYY-MM-DD`)는 사람이 번들의 이름과 버전을 마지막으로 확인한 날이다. 180일(`GUARDIAN_BUNDLE_MAX_AGE_DAYS`)이
  지난 번들은 `--package-bundles`의 `stale`에 나오고 `scripts/ci.sh config`가 경고한다. 오류는 아니고 전개도 그대로 되는데,
  오래된 목록은 설치를 멈출 이유가 아니라 한 번 보라는 신호이기 때문이다. 파일에는 지원 종료일을 적지 않는다. 아무도
  모르게 틀린 날짜는 날짜가 없는 것보다 나쁘다.
- 첫 번들은 `nginx`와 `postgresql`(apt, dnf, brew)이다. 패키지 목록은 운영자가 아직 확인해야 하는 첫 세트이다.

## 7. 액션과 계획

### 7.1 새 카탈로그 액션 (`ops_actions.py`)

| 도구 | 인자 | 위험 | preview | 실행 | verify |
|---|---|---|---|---|---|
| `ops:package_install` | `target_host`, `manager`(이름 또는 `auto`), `packages`(`name`과 선택적 `version` 1-20개) | medium. 단계가 "목록만"이거나 시뮬레이션이 서비스 유닛을 더하면 high | 시뮬레이션: 추가, 업그레이드, 삭제될 패키지 | 어댑터 `argv`, 비대화형 | 각 패키지가 그 버전으로 존재, 이전에 활성이던 서비스가 여전히 활성, 보호 패키지가 여전히 존재 |
| `ops:package_remove` | `target_host`, `manager`, `packages`(1-20), `purge?` | medium. 5개 넘게 지워지거나 서비스 유닛이 지워지면 high | 시뮬레이션 또는 의존 패키지 목록 | 어댑터 `argv` | 각 패키지가 없음, 같은 서비스와 보호 검사 |

두 액션 공통 규칙:

- 이름은 매니저별 정규식으로 검사한다(npm은 `@scope/name`을 받는다). 버전은 범위가 아니라 정확한 값이다.
- 출처는 항상 매니저에 설정된 레지스트리다. URL, git 참조, tarball이나 파일 경로, `--registry`, brew의
  `owner/tap/formula` 이름은 거부한다(`policy.allowed_taps`가 이름을 댄 tap만 예외).
- npm과 bun은 패키지 수명주기 스크립트를 평소대로 실행한다(운영자 결정, 2026-10-19: 기본 `--ignore-scripts` 없음).
  `ignore_scripts: true`면 스크립트 없이 설치한다. 스크립트는 임의 코드를 실행하므로 출처를 레지스트리로 제한하고(위),
  bun은 계속 high 위험으로 둔다.
- `manager: "auto"`는 검증기가 사실에서 노드의 기본 시스템 매니저로 풀어 해석된 값으로 저장한다. 찾지 못하면
  단계는 `INVALID`다.
- Drift: 실행 직전에 시뮬레이션을 다시 계산한다. 저장된 preview에 없던 패키지, 달라진 버전, 더 길어진 삭제 목록이
  있으면 `STALE`이 된다.
- `package_upgrade`는 이 설계에 포함되지 않는다.
- 두 액션 모두 `/api/exec/remote`를 거치며 감사 라벨은 `source: action`, `op`, `project`, `plan_id`다.

### 7.2 계획

판정자의 결정은 `actions` 옆에 `plan`을 가진다:

```json
{"plan": {"goal": "nginx on the web nodes",
          "steps": [
            {"id": "s1", "host": "node-1", "tool": "ops:package_install", "args": {"manager": "auto", "packages": [{"name": "nginx"}]}},
            {"id": "s2", "host": "node-1", "tool": "ops:restart_service", "args": {"unit": "nginx.service"}, "requires": ["s1"]},
            {"id": "s3", "host": "node-2", "tool": "ops:package_install", "args": {"manager": "auto", "packages": [{"name": "nginx"}]},
             "on_failure": "continue"}]}}
```

- 단계는 일반 `decision_actions` 행(상태 `PROPOSED`)이며 열 세 개(`plan_id`, `step`, `requires`(JSON))가 추가된다.
  액션에 이미 있는 모든 것(preview, `decided_by`, 감사, `STALE`)이 단계마다 적용된다.
- `requires`는 한 계획 안의 의존성이다. 단계는 필요한 모든 단계가 `verify.ok`와 함께 `EXECUTED`가 된 뒤에 돈다.
- 단계별 `on_failure`: `stop`(기본: 그 노드의 나머지 단계가 새 상태 `SKIPPED`가 되고 다른 노드는 계속) 또는
  `continue`. 계획 전체를 멈추는 `stop_plan`도 허용된다.
- 한도: 계획당 100단계, 노드당 20단계, 순환 없음, 프로젝트가 해석한 대상 노드만.
- 자동 롤백은 없다. 단계가 실패하면 실행기가 그 노드를 멈추고 역방향 단계를 사람이 승인할 새 `PROPOSED`
  액션으로 제안한다.

### 7.3 검증기 (코드, 모델 없음)

결정이 저장될 때와 계획이 승인을 위해 열릴 때 실행된다:

대상이 범위 안인가, 최신 사실에 매니저가 있는가, 필요한 곳에 sudo가 되는가, 이름과 버전의 형식, 시뮬레이션에 대한
보호 목록(6장), 순환 없는 그래프, 한도. 실패마다 단계를 한 줄 이유와 함께 `INVALID`로 표시하고, 토론 에이전트는 다음
라운드에서 그 이유를 본다.

## 8. 계획 실행

```
계획 승인 (모든 단계, 단계마다 결정 기록 하나, decided_by는 사람)
  노드별로 계획 순서대로, 노드끼리는 병렬 (한 번에 최대 2개):
    requires 대기 -> drift 검사 -> 실행 -> verify -> 기록
```

- `server-ops-project.ko.md` 7.4의 노드별 액션 잠금은 그대로다: 노드당 한 번에 한 단계.
- `policy.auto_run`은 패키지에 적용되지 않는다. 계획은 항상 사람이 승인한다.
- 대시보드는 계획을 노드별 표로 보여 준다: 단계, 보호 패키지, 시뮬레이션 결과, 위험. 새 문자열은 두 카탈로그
  (`ko`, `en`)에 모두 들어간다.

## 9. 팀과 프롬프트

새 팀 `fleet-provision`(분류 `operations`). 프로젝트 `fleet`의 `allowed_actions`에 두 도구가 추가되고
`fleet-ops`는 정리만 유지해서 기존 동작은 바뀌지 않는다.

- Expert `ops_provisioner`: 명령을 읽고, 이름이 나온 모든 노드에 `host_facts`와 `package_info`를 호출한 뒤
  노드별로 쓴다. 패키지 이름이 계열마다 다르므로(`apache2`와 `httpd`) 각 이름을 `package_info`로 확인한다.
- Critic: `ops_safety_critic`(재사용)과 새 `ops_dependency_critic`(보호 패키지, 연쇄 삭제, 혼합 fleet, 단계
  순서).
- 판정자 `ops_provision_judge`: 7.2의 계획 JSON을 쓴다.
- `RETRIEVAL_PROMPT_VERSION`을 올린다.

명령을 어느 팀에 보낼지는 토론 시작 대화상자에서 운영자가 계속 고른다. 명령 텍스트로 팀을 자동 선택하는 것은 이
설계에서 하지 않는다.

## 10. 단계

| 단계 | 내용 | 시험에 모델 필요 |
|---|---|---|
| P1 (완료, 2026-10-20 배포) | `host_facts`, `package_info`(apt, dnf, brew, npm), 기존 점검의 macOS 변형, 사실 캐시, `ops_roles` 필드 | 아니오 |
| P2 (완료, 2026-10-20 배포) | `ops_packages.py` 어댑터(apt, dnf, brew, npm, bun), `ops:package_install`, `ops:package_remove`, 내장 보호 목록, 단일 액션 검증기 | 아니오 |
| P2b (완료, 2026-10-20 배포) | zypper, apk, pacman, pnpm, yarn, pipx, uv tool | 아니오 |
| P3 (완료, 2026-10-20 배포) | 계획 객체, 열, 검증기, 계획 실행기, `SKIPPED`, 대시보드 계획 화면 | 아니오 |
| P4 (완료, 2026-10-20 배포) | `configs/package-roles.json`, 도출 보호, `policy.protected_packages`, 대시보드 역할 편집기 | 아니오 |
| P4b (완료, 2026-10-20 배포) | `configs/package-bundles.json`, `package_bundles.py`(`nginx`, `postgresql`), `--package-bundles`, CI 검사. 판정자는 P6부터 사용 | 아니오 |
| P5 (완료, 2026-10-20 배포) | 심은 결함 코퍼스(`package_corpus.py`, `artifact_corpus.py`와 같은 방식): 혼합 OS fleet, 보호 패키지 삭제, 연쇄 삭제, 낡은 시뮬레이션, sudo 없음. recall과 오차단율 측정 | 아니오 |
| P6 (구현 완료, 시험 토론 미실행) | 팀 `fleet-provision`, 페르소나 3개, `bundles` 점검, 판정자 프롬프트의 계획 규칙, `RETRIEVAL_PROMPT_VERSION` `p16`. 운영 노드에 닿기 전에 평가 샌드박스(`~/guardian-eval`)에서 하는 시험 토론 1회는 아직 남음 | 예 |

각 단계는 이 문서의 "구현 메모" 하위 절에 설계와 달라진 점을 기록한다. 테스트는 실제 데이터 디렉터리를 건드리지
않고 실제 패키지 매니저도 호출하지 않는다. 어댑터는 `argv`와 녹화된 출력 파싱으로 시험한다.

### 10.1 구현 메모

**P1 (2026-10-19, 코드는 `ops_facts.py`이고 `ops_checks.py`에 연결됨, 2026-10-20 배포).**

- 새 점검의 감사 라벨도 다른 점검처럼 `source=ops_check`이다. 5.1장은 처음에 `ops_mcp`라고 썼는데, 그것은 MCP 서버 조회
  경로의 라벨이고 `ops` 블록의 라벨이 아니다.
- `host_facts`는 `key: value` 줄을 출력하는 POSIX `sh` 스크립트 하나이다(노드에 python3나 jq가 없어도 된다).
  허브의 `parse_host_facts`가 파싱한다. 먼저 흔한 설치 디렉터리(`/opt/homebrew/bin`, linuxbrew, `~/.bun/bin`, nvm node)를
  `PATH`에 더한다. macOS의 비로그인 ssh 세션에는 이 경로가 없기 때문이다. 마지막 줄 `facts_complete: yes`가 완료 표시이다.
  이 줄이 없거나, 종료 코드가 0이 아니거나, 64 KiB에서 잘린 결과는 캐시하지 않는다.
- 수신 포트의 소유 프로세스는 root나 암호 없는 sudo가 있어야 보인다. 스크립트는 sudo가 되면 `sudo -n`을 쓰고
  `ports_owner_visibility: full|partial`을 출력한다. 소유자를 보지 못한 경우는 비어 있지 않고 `unknown`이다. P4의 도출
  보호는 `partial`과 `unknown`을 "보호할 것이 없다"가 아니라 "배제할 수 없다"로 읽어야 한다 (완료: P4 메모의 "사각지대").
- `host_facts`와 `package_info`는 자기 제한 시간(60초)이 있다. 서비스 소유 패키지는 서비스마다 `dpkg -S`/`rpm -qf`를
  한 번씩 실행하고, 서비스 목록은 60개에서 자른다.
- macOS 변형은 노드에서 `case "$(uname -s)" in Darwin) ...`으로 고른다. 빌더는 인자만 보고, 노드의 사실이 아직
  없을 수 있기 때문이다. Mac에서 실행해 확인했고, Linux 분기는 기존 명령 그대로이다. `uptime_load`는 표시 파일이
  없는 곳(dpkg도 `needs-restarting`도 없음)에서 "no reboot required" 대신 `reboot pending: unknown`이라고 답한다.
- `package_info`는 apt, dnf, brew, npm을 안다. 이름은 관리자별 패턴과 맞아야 한다(앞의 `-`, 버전, URL, 경로, tap,
  npm의 `@scope/` 외 스코프는 안 된다). dnf는 `-C`(캐시만)로 실행한다. bun의 조회 명령은 아직 넣지 않았다. bun이 있는
  노드에서 확인한 뒤 bun 어댑터를 쓸 때(P2) 넣는다. dnf 명령은 명령 문자열만 시험했고 실제 RPM 노드에서는 아직
  실행하지 않았다.
- 캐시는 `<data dir>/ops-facts/<host>.json`이다. 임시 파일과 `os.replace`로 원자적으로 쓰고, 노드 id는 파일명 패턴으로
  검사한다. P2와 P3은 `load_facts(host, max_age=900)`을 부른다.
- `ops_roles`는 `configs/servers.example.json`의 예시 필드이다. P1은 읽지 않았고(허브 에이전트와 `ops_targets`는 모르는
  인벤토리 필드를 무시했다) P4부터 읽는다. P4 메모를 본다.
- `RETRIEVAL_PROMPT_VERSION`은 `2026-10-19.p15`가 되었다(ops 프로토콜 문구가 바뀜).

**P2 (2026-10-19, 코드는 `ops_packages.py`이고 `ops_actions.py`, `mcp_client.py`, `action_ops.py`, `project_registry.py`에 연결됨, 2026-10-20 배포).**

- 패키지 도구는 프로젝트별 선택(opt-in)이다. ops 프로젝트의 기본 `allowed_actions`는 노드 도구에서 `ops:package_install`과
  `ops:package_remove`를 **뺀** 것이고, 프로젝트가 `policy.allowed_actions`에 직접 적어야 한다. `auto_run`은 여전히 저위험
  액션만 받으므로 패키지(중간 또는 높음)는 무인으로 실행되지 않는다. 기본 제공 `fleet` 프로젝트는 P6부터 둘을 나열하지만, 팀과
  페르소나도 나열한 판정자만 이를 본다(P6 메모 참조). 프롬프트 버전은 `2026-10-19.p16`이다.
- 노드 사실은 **캐시가 없거나 900초보다 오래되면 그 자리에서 읽는다**(`ops_actions.host_facts`, 감사 라벨
  `source=ops_check`). 완전한 사실만 저장한다. 그래서 판정자가 먼저 `host_facts`를 부르지 않아도 되고, 승인 시점에도 새
  사실로 액션을 다시 검증한다. 쓸 만한 사실이 없으면 액션을 거부한다.
- 검증기는 둘로 나뉜다. `shape`(관리자와 무관: 인자 이름, 패키지 1~20개, 평범한 이름, 중복 없음, 버전은 설치에만)는
  `ops_actions.validate`에서, `check`(사실 필요: `auto` 해석, 관리자 존재, apt/dnf의 root 또는 암호 없는 sudo(`sudo_required`),
  brew는 root 불가, `purge`는 apt만, `ignore_scripts`는 npm/bun만, 관리자별 이름·버전 패턴, 삭제에 대한 내장 보호 목록과
  `policy.protected_packages`)는 `MCPClient.validate`에서 실행하며 관리자가 정해진 인자를 돌려준다.
- 시뮬레이션이 거부할 수 있다. `assess`는 삭제가 보호 패키지도 함께 지우거나(brew) 설치된 formula가 그 패키지에 의존하면
  `refusal`을 돌려준다. 제안 시점에는 액션이 `INVALID`로 저장되고, 실행 시점에는 측정 결과 때문에 `execute`가 실행 없이
  `FAILED`를 돌려주며, `drift`는 이를 낡은 것("현재 상태로 다시 토론")으로 보고한다. 설치 시뮬레이션이 무언가를 삭제하면
  위험도는 `high`이다.
- 위험도는 기본 `medium`이다. bun, 삭제 6개 이상(요청 또는 시뮬레이션), 무언가를 삭제하는 설치는
  `high`이다. npm과 bun은 `ignore_scripts`가 없으면 패키지 스크립트를 실행한다(운영자 결정 2026-10-19. 초안은 기본이 스크립트 끔이고 `allow_scripts`로 켰다).
- 미리보기와 검증: 측정은 네 구역(설치된 버전, 시뮬레이션, 종료 코드, 실행 중 서비스)이고 4000자 절단 전에 파싱한다.
  종료 코드가 0이고, 모든 패키지가 요청한 버전으로 설치되어 있거나(삭제면 없거나), 실행 전에 돌던 서비스가 멈추지 않았을
  때만 검증이 성공한다.
- 출력 형식의 출처: npm(`--dry-run`의 `add/change/remove` 줄, `ls -g --json`)과 brew(`--dry-run`, `name old -> new`
  업그레이드, `brew uses`)는 Mac에서 실행해 확인했다. apt는 그 뒤 운영자가 시험을 허락한 Ubuntu 24.04 x86_64 fleet 노드에서 실행했다(2026-10-19). 실제 사실 수집, 설치와
  없는 패키지의 시뮬레이션 파싱이 기대대로였고, 보호 패키지 삭제는 거부되었으며, `tree`를 `ops:package_install`로
  설치하고 `ops:package_remove`로 지우는 실제 `execute`가 검증 `ok`인 `SUCCESS`로 끝났고 노드는 원래대로
  돌아갔다. **dnf(트랜잭션 표, 줄바꿈된 이름, `--assumeno` 뒤 종료 코드 1)는 도구의 문서화된 형식으로 썼고 실제
  노드에서 실행한 적이 없다.** P6 전에 dnf 노드 시험 실행이 필요했으나, 2026-10-19에 운영자가 apt 노드부터 쓰기로 결정했다(P6 메모 참조).
- **bun은 검증되지 않았다.** 어댑터는 있지만, bun이 있는 노드가 없어 명령과 출력을 확인하지 못했으므로 허브 환경에
  `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`가 `bun`을 나열하지 않으면 거부한다.
- Mac에서 `HOMEBREW_NO_AUTO_UPDATE=1` 없이 brew를 조사하다가 그 Mac의 tap이 한 번 갱신되었다(해롭지 않고, 운영용
  전처리 코드는 이 변수를 설정한다). URL, git, tarball, tap, 버전 범위는 거부한다.

**P2b (2026-10-19, 코드는 P2와 같은 파일에 `ops_packages.py` 표를 더함, 2026-10-20 배포).**

- 추가된 매니저: zypper, apk, pacman(시스템, root 또는 비밀번호 없는 `sudo -n`)과 pnpm, yarn, pipx, uv(사용자
  수준). Linux 노드의 `auto` 결정은 apt와 dnf 다음에 이들을 찾는다. `ops_facts.package_info`는 확장하지 **않았다**:
  여전히 apt, dnf, brew, npm만 안다.
- **실제 실행으로 확인**(2026-10-19, 운영자가 시험을 허락한 노드에서, 임시 디렉터리를 쓰고 끝난 뒤 지움. 노드 이름은
  여기에 적지 않는다): pnpm 9, yarn 1.22, pipx 1.18, uv 0.13의 설치, 목록, 삭제. **어느 실제 노드에서도 확인하지
  못한 것: zypper, apk, pacman, bun**(노드도 컨테이너 런타임도 없었다). 이들의 어댑터는 문서화된 형식으로 썼고
  `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`에 나열하지 않으면 거부된다. dnf도 아직 실행하지 못했다.
- 실제 실행에서 설계를 바꾼 발견:
  - `yarn global list`는 바이너리가 없는 패키지를 숨기므로 설치된 버전은
    `$(yarn global dir)/node_modules/<name>/package.json`에서 읽는다. `global`은 yarn 1.x에만 있으므로 다른 버전에서는
    명확한 메시지와 함께 종료 코드 3으로 끝난다.
  - pnpm은 전역 명령 전에 `PNPM_HOME`과 맞는 `PATH`가 필요하다. 전처리 코드가 OS별로 둘 다 설정한다.
  - `pipx`와 `uv tool`은 **액션당 패키지 하나**만 받는다(각각 자기 환경을 만들고, 이름이 둘이면 어느 쪽이 실패했는지
    가려진다). 이미 설치된 패키지에 `pipx install`은 아무 일도 하지 않으므로 검증은 버전을 비교한다. Python 이름은
    PEP 503 정규화 형태로 비교한다. 버전 지정은 `==`를 쓴다.
- "목록만" 매니저(bun, pnpm, yarn, pipx, uv)에는 dry run이 없다. preview는 요청과 실제 명령이고 위험도는 `high`다.
  zypper(`--dry-run`), apk(`--simulate`), pacman(`-S/-R --print --print-format "%n %v"`)은 시뮬레이션하며, 이때
  시뮬레이션도 권한 접두어 아래에서 실행한다.
- pacman: `-y` 없음(전체 업그레이드 없는 데이터베이스 갱신은 부분 업그레이드) 그리고 버전 고정 없음. zypper 종료
  코드 102, 103(재부팅 또는 재시작 안내)은 실제 실행에서 성공으로 센다. 시뮬레이션된 삭제에 보호 패키지가 들어
  있으면 다른 매니저와 같이 `refusal`이다.
- `ignore_scripts`는 npm, bun, pnpm, yarn에서만 받고 나머지에서는 거부한다. 스크립트는 기본으로 실행된다.
  "기본 `--ignore-scripts` 없음"을 opt-in으로 읽은 것은 어시스턴트의 해석이며 운영자가 확인하지 않았다.

**P3 (2026-10-19; `plan_ops.py`, `plan_handler.go`, `PlanDialog.tsx` / `PlansPanel.tsx`; 2026-10-20 배포).**

- **저장.** 테이블 `decision_plans`(id, thread_id, round_num, project_id, goal, stop_plan, created_at)와
  `decision_actions`의 열 네 개(`plan_id`, `step`, `requires`, `on_failure`). 기존 DB는 그 자리에서 이전된다. 계획 id는
  `plan_<스레드>_r<라운드>`다. 단계는 보통의 액션 행이므로 승인 목록, 감사, `decided_by`, jev 게이트가 다른 액션과 같게
  본다.
- **검증 두 단계.** 구조 오류(형태, 중복 단계 id, 순환, 100단계 초과 또는 노드당 20 초과)는 계획 전체를 거부하고
  아무것도 저장하지 않는다. 노드에서 검증을 통과하지 못한 단계는 사유와 함께 `INVALID`로 저장되고 계획은 계속 검토할
  수 있다. 7.3("단계를 INVALID로 표시")과 다른 점은 계획을 여전히 실행할 수 있다는 것뿐이다. `INVALID`는 실패로 세므로 그
  단계에 의존하는 단계는 `SKIPPED`가 된다.
- **Preview.** 단계는 계획을 저장할 때 측정한다. `requires`가 아직 실행되지 않은 단계는 정직하게 측정할 수 없으므로(그
  사이 노드가 달라진다) preview를 `deferred`로 저장하고 실행 직전에 측정한다. 이때 drift 검사는 비교할 대상이 없어
  거부 규칙만 적용한다. 대시보드가 이를 알려 준다.
- **실패의 의미.** `FAILED`, `STALE`, `INVALID`는 실패이고, `REJECTED`(사람의 선택)와 `SKIPPED`(결과)는 아니다.
  `on_failure: stop`(기본)은 그 노드의 나머지를 건너뛰고, `continue`는 계속 가며, `stop_plan`은 시작하지 않은 모든
  노드의 모든 단계를 건너뛴다. `requires` 중 하나가 실패했거나 건너뛰어졌으면 그 단계도 건너뛴다. **롤백은 없고 역방향
  단계도 제안하지 않는다**(7.2에 언급이 있으나 만들지 않았고, 11장의 열린 점은 그대로다).
- **실행.** 노드는 병렬(동시 2개), 노드당 한 단계(server-ops 7.4의 노드별 잠금)다. 바쁜 노드는 120초 기다린 뒤
  단계가 `not_run`으로 실패한다(시작하지 않았다). 계획 자체도 잠겨 한 번에 한 실행만 가능하다. 중단된 실행은 이어서 할 수
  있다: `PROPOSED` 단계만 시작하고 끝난 단계는 상태를 유지한다. `policy.auto_run`은 계획 단계를 실행하지 않는다. 계획
  단계는 단독으로 실행하거나 거절할 수 없다(`CONFLICT`, 대시보드는 버튼을 숨긴다). `HELD` 단계가 있으면
  `--jev-recheck` 전까지 계획이 막힌다.
- **CLI.** `--plans`, `--plan-show`, `--plan-run ID --yes`, `--plan-reject`(모두 `--json`, 종료 코드 2/3/4는 400/404/409).
- **허브.** `GET /api/plans[?thread=&project=]`, `GET /api/plans/{id}`, `POST /api/plans/{id}/run`(본문
  `{"confirm_id": "<계획 id>"}`)과 `/reject`. 계획은 몇 시간 걸릴 수 있어 실행을 요청에서 분리했다. 3초 뒤에도 실행 중이면
  허브가 `202 {"started": true}`로 답하고 대시보드가 3초마다 계획을 조회한다. 유예 안에 끝나면 결과로 답한다. 계획당
  프로세스 내 실행은 하나(두 번째는 409). `GUARDIAN_UI_ACTIONS=off`는 실행과 거절을 막고 조회는 막지 않는다.
- **대시보드.** 승인 목록 위의 계획 패널(계획이 없으면 숨김). `#/approvals/plan_...`이 계획 대화상자를 연다: 노드별
  단계, 시뮬레이션, requires / on-failure, 실행(낮음·중간 위험은 체크박스, 그 외는 계획 id 입력), 거절. 계획 단계의 액션
  대화상자는 계획과 함께 실행된다고 설명하고 계획으로 가는 링크를 둔다.
- **하지 않은 것 / 알려진 빈틈.** 감사 로그에 `plan_id` 라벨이 없다(단계는 단일 액션처럼 라벨이 붙는다). fail-stale 실시간
  검사(`failStaleAction`)는 계획 실행을 모르고, 늦은 결과는 `late_result`로 기록된다. 프롬프트는 P6 전에는 `plan`을 언급하지
  않았고, P6부터 프로비저닝 judge가 계획을 쓴다(P6 메모 참조).

**P4 (2026-10-19, 코드는 `package_roles.py`와 `configs/package-roles.json`이고 `ops_packages.py`, `ops_actions.py`, `mcp_client.py`, `ops_targets.py`, `project_registry.py`, Go, 대시보드에 연결됨, 2026-10-20 배포).**

- **네 가지 출처를 합친다.** 내장 `ops_packages.PROTECTED` 목록(풀 수 없음), 노드의 역할(`ops_roles` ->
  `configs/package-roles.json`), 노드에서 실제로 도는 것(사실에서 도출), 프로젝트의 `policy.protected_packages`(더하기만
  가능). 역할은 `web`, `database`, `gitea`, `headscale`, `llm_cli_host`, `build`, `monitoring`으로 시작한다. 패키지
  목록은 운영자가 아직 확인해야 하는 첫 세트이다.
- **저장하지 않고 다시 계산한다.** 설계는 보호 집합을 한 번 계산한다고 했다. 실제로는 검증할 때마다 계산한다. 액션을
  제안할 때(`validate`), 미리보기를 측정할 때(`ops_preview`), 실행 직전(`execute_action`)에 그 시점의 사실 캐시, 인벤토리,
  정책으로 만든다. 제안 뒤에 역할이 추가되거나 서비스가 시작돼도 실행이 막힌다. 미리보기는 `protected`("패턴 (출처)"
  줄)를 담아 승인 화면이 이유를 말할 수 있게 한다. 대시보드는 이 필드를 아직 보여주지 않는다.
- **모르는 역할은 거부한다.** `ops_roles`에 있는데 파일에 없는 역할은 인벤토리를 고칠 때까지 그 노드의 모든 패키지
  액션을 거부한다. 오타 난 역할이 아무도 모르게 아무것도 보호하지 않는 일을 막기 위해서이다. 역할 파일을 읽을 수 없거나
  잘못되면 모든 노드에서 패키지 액션을 거부한다.
- **도출 보호는 좁다.** 실행 중인 모든 서비스의 소유 패키지와 수신 포트의 소유자를 쓰고, OS 계열의 시스템
  매니저(Linux의 apt/dnf/...)에만 적용한다. npm이나 brew에는 적용하지 않는다. 평범한 문자열이 아닌 이름과 `unknown`은
  건너뛴다.
- **사각지대(P1이 남긴 빈틈을 닫는다).** Linux `host_facts` 스크립트가 포트마다 수신 중인 바이너리를 패키지로 풀어
  (`ss -p`의 pid, `/proc/<pid>/exe`, `dpkg -S` / `rpm -qf`) `port: ADDR proc=NAME pkg=PKG`를 출력한다. 그래서 `postgres`라는
  프로세스가 `postgresql-16`을 보호한다. 예전 형식(`pkg=` 없음)으로 저장된 사실은 프로세스 이름으로 대신한다.
  `package_roles.blind_spots()`는 보호 목록이 보지 못하는 것을 이름 붙인다: 소유자를 못 찾은 포트, 그리고 `full`이 아닌
  `ports_owner_visibility`. 이유는 측정된 미리보기의 `blind`로 전해지고, `ops_packages.risk()`는 이런 노드의 **삭제**를
  `high`로 올린다(설치는 올리지 않는다). 대시보드는 액션 창과 플랜 보기에서 시뮬레이션 옆에 이유를 보여 준다. 실제 노드에서
  스크립트를 돌려 찾은 함정 둘: 소켓 활성화 서비스(sshd, rpcbind)는 pid 1 아래로 나오는데 그 패키지는 init 시스템이라 서비스에
  대해 아무것도 말해 주지 않는다. 그래서 그런 포트는 `systemd`를 보호하지 않고 `unknown`(곧 사각지대)으로 둔다. 또 업그레이드로
  바뀐 바이너리의 `/proc/<pid>/exe`는 ` (deleted)`로 끝나서 패키지 조회 전에 떼어 낸다. 패키지가 없는 서비스는 사각지대가
  아니다. 이름으로 나열된다. 이것은 거부가 아니라 등급이다. 삭제는 사람이 승인한다.
- **프로젝트 추가분은 모든 매니저에 적용된다.** `policy.protected_packages`는 `"*"` 키로 저장한다. 패턴은 패키지 이름에
  대한 fnmatch이다(글자, 숫자, `. _ + @ / -`, `*`와 `?`, 리터럴 한 글자 이상, 최대 100개).
- **인벤토리.** `ops_roles`를 이제 읽는다. `ops_targets.resolve`가 `roles`를 돌려주고 `host_roles()`가 나열한다. 허브 에이전트가
  검증(`metaRoleRe`, 소문자 1-32자)하고 `FleetNode`와 노드 메타데이터에 보관한다. `GET /api/package-roles`는
  `guardian.py --package-roles`(역할, 설명, 패턴)를 대시보드에 준다.
- **대시보드.** 노드 폼에 알려진 역할을 힌트로 보여주는 "패키지 역할" 목록 필드가, ops 프로젝트 폼에 보호 패키지 입력란이
  생겼다. 문구는 `ko`와 `en` 두 카탈로그에 있다.
- **CI.** `scripts/ci.sh config`가 `guardian.py --package-roles`를 실행하고 역할 파일이 잘못되면 실패한다.
- **하지 않은 것.** `preview.protected`는 대시보드 단계 상세에 아직 보이지 않는다. 역할이 나열하지 않은 매니저에는 보호
  패턴이 없다(역할 작성자가 떠올린 매니저만 적는다).

**P4b (2026-10-19, 코드는 `package_bundles.py`와 `configs/package-bundles.json`, 2026-10-20 배포).**

- 6.3장이 새로 생겼다. 설치 번들은 역할 파일과 일부러 분리했다(거부권과 제안). P4는 바뀌지 않았고, 역할은 여전히 아무것도
  설치하지 않는다.
- `guardian.py --package-bundles`가 파일을 나열하고 검증한다(JSON, 잘못되면 종료 코드 2). `scripts/ci.sh config`는 잘못된
  파일이면 실패한다. 패키지 이름은 매니저별 `ops_packages.NAME_PATTERNS`로 검사하므로 검증기가 거부할 이름은 번들에 들어갈
  수 없다. 테스트는 전개한 모든 번들을 맞는 사실(apt, dnf, brew)에서 `MCPClient.validate`에 넘기고, `web`/`database` 역할이
  `nginx`/`postgresql`의 필수 패키지를 보호하는지도 확인한다.
- **P6에서 판정자에 연결했다.** 읽기 전용 점검 `bundles`가 번들을 나열하고 매니저별로 전개한다(P6 메모 참조).
  `GET /api/package-bundles`와 대시보드 화면은 없다.
- `postgresql@17`은 brew 포뮬러이다(로컬 brew로 확인). 더 새로운 메이저 버전은 새 항목이 필요하다.
- **`reviewed`와 버전 정책(2026-10-19, 같은 단계).** 모든 번들에 필수 `reviewed` 날짜가 있고, `expand`가 그것을 돌려주며,
  `--package-bundles`는 `max_age_days`와 `stale`(오래된 순)을 더한다. CI는 경고만 한다. 두 번들 모두 apt와 dnf는 배포판의
  버전을 따른다는 노트를 달고 있다.

**P5 (2026-10-19, 코드는 `package_corpus.py`, 테스트는 `test_package_corpus.py`, 2026-10-20 배포).**

- 정상 동작 10개와 심은 결함 30개, 종류마다 6개. 혼합 OS fleet(노드에 없는 매니저, 운영자가 켜지 않은 미검증 매니저,
  root로 brew, apt 밖의 `purge`), 보호 패키지 삭제(내장 목록, 역할, 실행 중인 서비스, 열린 포트, 프로젝트 정책, 맹점
  등급), 연쇄 삭제(보호된 의존 패키지, 프로젝트가 보호한 패키지, 실패한 pacman 트랜잭션, 5개 초과 삭제, 삭제를 부르는
  설치), 낡은 시뮬레이션(새 삭제 항목, 서비스나 역할 때문에 보호된 패키지, 사라진 패키지, 실패하는 시뮬레이션), sudo 없음
  (매니저 4종, facts 없음, 제안과 실행 사이에 잃은 sudo)이다.
- `decide()`는 hub와 같은 함수를 같은 순서로 부른다. `package_roles.protection`, `ops_packages.check`,
  `ops_packages.assess`, `ops_packages.risk`, 실행 시점에는 `ops_actions.drift`까지. 모델도 노드도 패키지 매니저도
  실행하지 않고 데이터 디렉터리도 건드리지 않는다. `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`는 시나리오마다 지정하고 되돌린다.
- 결함은 게이트가 그 종류가 기대하는 이유(`needle`)로 거부할 때만 잡힌 것으로 센다. 거부가 아니라 등급이 방어선인
  경우에는 행동이 가능하지만 `high`로 평가되고 정상 기준은 그렇지 않을 때만 센다. 기준 시나리오가 이미 거부되면 결함이
  잡힌 것처럼 보일 수 없다. 오차단율은 거부되거나 등급이 달라진 정상 동작의 비율이고 0.00이다.
- 첫 측정은 모든 종류에서 recall 1.00(30개 중 30개), 오차단율 0.00이다. 처음 실행은 30개 중 28개였고, 놓친 둘은 게이트가
  아니라 시나리오의 결함이었다(`error:` 표지가 없는 pacman 오류 줄, 패키지가 설치돼 있지 않던 역할 시나리오). 위 수치
  전에 고쳤다. `python3 package_corpus.py --targets`는 모든 종류를 1.00으로, 오차단율을 0.00으로 묶고, CI는 단위
  테스트의 일부로 실행한다.
- **이 수치가 말하지 않는 것.** 시뮬레이션은 기록된 형식(apt, npm, brew)과 문서화된 형식(dnf, zypper, apk, pacman)이다.
  실제 노드가 다르게 출력하는 형식은 코퍼스의 사각지대이며, 그래서 dnf 노드 실행이 실행하지 않았고, 운영자가 2026-10-19에 이를 받아들여 사용을 apt 노드로 한정했다. 코퍼스는
  게이트를 재는 것이지 판정자가 좋은 플랜을 제안하는지를 재는 것이 아니다.

**P6 (2026-10-19, 구현 완료, 2026-10-20 `6b24987`로 배포, 시험 토론 미실행).**

- **팀 `fleet-provision`**(분류 `operations`, 2라운드): 전문가 `ops_provisioner`, 비평가 `ops_provision_safety_critic`과
  `ops_dependency_critic`, 판정자 `ops_provision_judge`. 팀 `allowed_tools`는 `ops:package_install`,
  `ops:package_remove`, `ops:restart_service`이고, 페르소나 `mcp_capabilities`도 같은 셋이다.
- **세 관문이 모두 일치해야 한다.** 판정자는 팀 목록, 페르소나, 프로젝트 `policy.allowed_actions`가 모두 도구를 나열할
  때만 그것을 받는다(`team_manager.judge_tools`, 이어서 `MCPClient.project_tools`). 기본 제공 `fleet` 프로젝트는 이제
  패키지 도구 둘을 허용하지만 `fleet-ops`(페르소나 `ops_judge`)는 여전히 받지 못하고, 새 ops 프로젝트(기본 정책에 패키지
  도구 없음)에서는 프로비저닝 판정자에게 재시작만 남는다. 세 경우를 테스트한다.
- **프롬프트 관문.** `DebateEngine`은 판정자의 도구에 패키지 도구가 있을 때 `provisioning`을 켠다. 그때만
  `ops_checks.protocol_text(True)`(`bundles` 점검과 "Package orders" 규칙: `host_facts`, 호스트별 `package_info`, 번들 우선,
  `[#n check host]` 인용)와 `JUDGE_PLAN_RULES`(주문은 `actions`가 아니라 같은 JSON 블록의 `plan` 키에 넣는다)가 토론 에이전트에
  전달된다. 플래그 없는 `ops_checks.protocol_text()`는 P6 이전과 바이트 단위로 같으므로 `fleet-ops` 프롬프트는 바뀌지
  않았다. `RETRIEVAL_PROMPT_VERSION`은 프로비저닝 프롬프트가 새로 생겨 `2026-10-19.p16`이다.
- **`bundles` 점검.** 허브 로컬이고 호스트가 없다. 인자 없이 번들을 나열하고, `name`과 `manager`를 함께 주면 하나를
  전개한다(`package_bundles.expand`). 호출이 호스트 없이 처리되므로 인용은 어떤 호스트 토큰으로도 검증된다. 패키지 도구가
  없는 라인업에는 이 점검이 알 수 없는 것으로 취급된다.
- **테스트**(`test_provisioning.py`, 13개): 세 경우의 도구 관문, 팀 파일, 점검과 오류, 프롬프트 예시 계획의
  `parse_plan`·`normalize_plan` 통과, 가짜 러너로 엔진을 끝까지 돌리는 프롬프트 관문.
- **하지 않은 것.** `~/guardian-eval`에서의 시험 토론은 실행하지 않았다. 운영과 공유하는 Codex 할당량을 쓰므로 운영자의
  허락이 필요하다. `fleet-provision` 외의 허브 팀은 패키지를 제안할 수
  없다. 2026-10-20 `6b24987`로 배포했다. `--ignore-scripts` 해석과 번들 구성(선택 추가 패키지, `postgresql@17`)은 운영자의 확인을 기다린다.
- **결정(2026-10-19, 운영자): apt 노드만, dnf 검증 없음.** dnf 시험 노드가 없으므로 `fleet-provision`은 apt 노드에서 먼저
  쓰고 dnf 노드는 검증되지 않은 채로 둔다. 코드는 이를 강제하지 않는다(dnf는 `ops_packages.UNVERIFIED`에 없다). 안전장치는
  패키지 액션이 자동 실행되지 않는다는 것(중간 또는 높음 위험)과, 운영자가 모든 단계의 미리보기를 읽고 승인한다는 것이다.
  `dnf`를 `UNVERIFIED`에 넣으면 `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`에 적지 않는 한 허브가 dnf 노드를 거부한다. 원하면 쓸 수 있는
  fail-closed 선택지이다.
- **첫 실사용과 보정(2026-10-19, 운영자 지적: 방어가 너무 강하다).** 주문은 "서버들의 timezone과 패키지를 확인해보자, 서버
  관리해보자"였고(`thread_20261019_142607_2ed0f3`), 결과는 액션 없이 APPROVED에 아무도 요청하지 않은 `constraints` 다섯 개("사용자가
  존을 정하기 전에는 5대를 바꾸지 않는다", "UTC를 기본값으로 정하지 않는다", "재시작은 한 번에 하나", "한 대로 먼저 시험", "소유
  패키지는 건드리지 않는다)였다. 원인: 안전 비평가가 "더 안전한 대안"만 내도록 되어 있어 모든 지적이 제동 쪽으로 쏠렸고, 판정자가
  비판을 구속 조건으로 그대로 채택했으며("두 비판 수용"), 전문가가 던진 열린 질문(목표 존)이 보류로 굳었다. 결과의 일부는
  맞았다(주문이 *확인*이었고, 시간대 변경은 이 팀의 도구 밖이다). 문제는 판정자가 그것을 "이 도구로는 못 한다, 이 명령을 직접
  실행하라"로 말하지 않고 "보류"로 포장한 점이다. 변경(페르소나만, `RETRIEVAL_PROMPT_VERSION` 그대로, `fleet-ops` 불변):
  프로비저닝 전용 `ops_provision_safety_critic`(기존 `ops_safety_critic`은 `fleet-ops`에 남는다), 두 비평가는 지적마다 `[차단]`
  (인용된 구체적 피해)이나 `[권고]`를 붙이고 주문이 요구하지 않은 사전 조건을 걸 수 없다, 판정자는 `[차단]`만 계획에 반영하고
  권고는 checks로 적으며 새 금지 조건을 만들지 않고 도구로 못 하는 일은 직접 실행할 명령과 함께 밝힌다, 전문가는 권장 기본값을 단
  질문 하나와 함께 준다. 모델로는 아직 측정하지 않았다. 다음 실제 주문이 시험이다.

## 11. 열린 점

- 역할 이름과 패키지 목록(6.2)은 작게 시작한다. 첫 세트는 운영자가 확인한다.
- "목록만" 도구(bun, pipx)가 약한 preview를 가지고 있는데, P6 전에도 허용할지.
- 설치가 실패했을 때 승인 없이 자동 역방향 단계를 제안할지(지금은 아니오로 결정).
- "burn"은 `bun`으로 가정했다(4장).

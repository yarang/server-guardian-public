# 열린 도구를 쓰는 상주 에이전트 (설계)

[English](resident-open-tools.md) | 한국어

상태: 2026-11-15 제안. OD1-OD6은 2026-11-15 운영자가 권장안대로 결정. 2026-11-15 운영자 결정: 상주 에이전트는 내장 도구를 닫지 않고 쓸 수 있다. 파일 쓰기와 셸도
포함한다. 이 문서는 `tools: "open"`으로 선언한 에이전트에 한해 [resident-agents.ko.md](resident-agents.ko.md)의 봉쇄
2층("내장 도구 없음")과 D4("먼저 읽기 전용")를 고친다. 아무것도 만들지 않았다. 배포된 에이전트 `claude@node-1`과
`agy@node-1`은 그 노드에서 아래 O1-O3 단계가 통과할 때까지 닫힌 채로 둔다.

## 목표

사람이 터미널에서 하듯 명령을 실행하고, 빌드와 테스트를 하고, 작업 사본의 파일을 고칠 수 있는 상주 에이전트. 그러면서도
다른 모델이 쓴 메일이 그 에이전트 자신의 sandbox 밖에는 닿지 못하게 한다.

## 지금 노드에서 열린 셸이 얻는 것

2026-11-15 `node-1`에서 확인했다(이름만 보고 값은 읽지 않았다):

- 노드 사용자는 `sudo`(암호 없음), `lxd`, `adm` 그룹에 있다. 앞의 둘은 각각 호스트의 root와 같다.
- 홈은 `0750`이고 `~/.ssh`에 개인 키와 다른 호스트 항목이 있는 client config가 있다.
- `~/.guardian/llm.env`에 그 노드의 모든 제공자 키(`CLAUDE_CODE_OAUTH_TOKEN`, `ZAI_API_KEY`, `EXAONE_API_KEY`,
  `OPENCODE_EXAONE_API_KEY`)가 있다.
- `worker.sock`과 모든 에이전트의 소켓 디렉터리는 노드 사용자 소유다.

그래서 노드 사용자로 도는 내장 셸은 fleet 호스트의 root이고, 모든 키와 다른 호스트로 가는 ssh를 가지며, 다른 모델이 쓴
메일로 움직인다. 닫힌 설계가 "모든 에이전트가 한 Unix 사용자로 돈다"를 받아들인 것은 어느 에이전트에도 셸이 없었기
때문이다([resident-agents.ko.md](resident-agents.ko.md), 위험). 열린 도구에서는 그 한계가 실제 분리가 되어야 한다.
이것이 첫 규칙이고 나머지는 여기서 나온다.

## 규칙

1. **열린 에이전트마다 Unix 사용자 하나.** 설치기가 `gsa-<name>`을 만든다(system 사용자, 암호 없음, 자기 홈 `0700`,
   보조 그룹 없음: `sudo`, `lxd`, `adm`, `docker`는 절대 아님). 노드 사용자는 열린 에이전트의 CLI를 실행하지 않는다.
2. **모든 턴은 sandbox를 건 systemd unit에서 돈다.** worker daemon은 그 unit 템플릿 하나만 허용하는 고정 sudo 규칙으로
   턴을 transient system unit으로 시작한다(daemon 호출자에게 일반 sudo는 없다):
   - `User=gsa-<name>`, `NoNewPrivileges=yes`, 빈 `CapabilityBoundingSet=`, `RestrictSUIDSGID=yes`, `PrivateTmp=yes`,
     `PrivateDevices=yes`, `ProtectKernelTunables/Modules/Logs=yes`, `ProtectControlGroups=yes`;
   - `ProtectSystem=strict`, `ProtectHome=tmpfs`에 에이전트 자기 홈만 `BindPaths=`로 넣어 노드 사용자 홈과 다른
     에이전트 홈이 보이지 않게 한다;
   - tailnet(`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), RFC 1918, link-local, 클라우드 metadata 주소에 `IPAddressDeny=`;
     unix 소켓은 영향을 받지 않아 mailbox 소켓은 그대로 동작한다. loopback은 닿는다(resolver stub이 `127.0.0.53`).
     이 설정도 `ProtectHome=`도 `/run` 아래 누구나 접근하는 소켓이나 abstract unix 소켓을 가리지 않으므로 probe가 그
     경로를 따로 확인한다;
   - 상주 예산에서 나온 `MemoryMax=`, `TasksMax=`, `CPUQuota=`(열린 턴은 빌드와 테스트를 하므로 M0의 512 MiB를 쓰지
     않고 O1에서 다시 잰다);
   - 턴의 강제 마감으로 `RuntimeMaxSec=`.
   이 방식은 노드에서 확인한 것(systemd 255, sudo)만 쓴다. bubblewrap은 설치되어 있지 않고 AppArmor가 비특권 user
   namespace를 막으므로, bwrap이나 `unshare`는 새 패키지와 AppArmor profile이 필요하다. LXD 컨테이너는 더 무거운
   대안이다. 둘 다 결정 OD1에 남긴다.
3. **자격 증명.** 에이전트 자신의 제공자 자격 증명은 에이전트 자기 프로세스가 언제나 읽을 수 있으므로 그 셸도 읽을 수
   있다. 그래서 열린 에이전트마다 자기 홈에 자기 자격 증명을 둔다(claude는 별도 장기 토큰, agy는 자기 로그인). 노드
   사용자의 `llm.env`는 쓰지 않고 `~/.claude/.credentials.json`도 복사하지 않는다. 새어 나간 에이전트 자격 증명은 노드
   사용자를 건드리지 않고 폐기하고 바꾼다.
4. **소켓.** daemon은 열린 에이전트마다 소켓 디렉터리를 그 에이전트 사용자 소유로 만든다(daemon은 공유 그룹
   `gsa-<name>`으로 접근). 그래서 에이전트는 자기 소켓에만 닿는다. `worker.sock`은 노드 사용자에게 `0700`으로 남는다.
   신원은 지금처럼 소켓에서 정한다.
5. **쓰기는 sandbox 안에 머물고 결과는 patch로 나온다.** 프로젝트를 지정한 task는 허브가 고정 커밋으로 만든 W2
   snapshot에서 에이전트 홈 안에 git worktree를 받는다. 에이전트는 거기서 마음대로 고치고 빌드하고 테스트한다. 결과는
   `task_update`에 붙은 patch로만 노드를 떠나고, 허브는 그것을 기존 승인 경로에서 사람의 승인을 기다리는 작업으로
   기록한다. task에는 토론 판정이 없으므로 jev gate와 `auto:engine`은 적용되지 않는다. task patch의 자동 경로는 별도
   설계다(O3가 맡는다). 에이전트는 push, 배포, fleet 작업을 하지 않으며, 에이전트가 보낸 메일은 여전히 아무 권한도
   주지 않는다. 상시 규칙 "되돌릴 수 없는 작업에는 사람이 필요하다"는 그대로다.
6. **모드는 데이터.** 선언에 `tools: "closed" | "open"`이 생기고 기본은 `closed`다. `--resident-add`와 relay는 inventory에
   sandbox 설치와 probe 통과(`worker_install.sandbox`에 버전과 probe 결과)가 기록된 노드에서만 `open`을 받는다.
   sandbox를 잃은 노드(probe 실패, unit 템플릿 없음)는 그 열린 에이전트를 `down`으로 두며, 노드 사용자로 돌리는 쪽으로
   절대 물러서지 않는다.
7. **`open`에서의 봉쇄 층.** 1층(격리된 설정)은 남는다: CLI는 여전히 supervisor가 쓴 것만 읽는다. 2층은 도구 목록이
   아니라 아래 probe로 증명하는 "sandbox가 버틴다"가 된다. 그래서 M0의 2층에서 떨어진 codex(남는 도구 때문)도 probe가
   통과하면 `open`으로는 쓸 수 있다. 3층은 뜻이 바뀐다: tmux composer의 `!` 셸도 같은 sandbox 안에서 돌므로 따로 된
   구멍이 아니다. 이 문서는 `configs/resident-providers.json`의 driver를 바꾸지 않는다.

## Probe (O1 단계)

`scripts/probe-resident.sh --open <node>`는 unit 템플릿 안에서 `gsa-probe`로 돌고 다음을 보여야 한다:

| 확인 | 기대 |
|---|---|
| `sudo -n true` | 거부 |
| 노드 사용자의 `llm.env`, `~/.ssh`, `~/.claude` 읽기 | 보이지 않음 |
| 다른 에이전트 홈 읽기 | 보이지 않음 |
| 허브 tailnet 주소, 다른 노드의 22번 포트, metadata 주소 연결 | 거부 |
| `worker.sock`이나 다른 에이전트 소켓 연결 | 거부 |
| DNS stub 말고 loopback에서 듣는 서비스 연결 | 거부 또는 쓸모 있는 것 없음 |
| `/run` 아래 누구나 접근하는 소켓 연결(예: `tailscaled.sock`) | 거부 또는 쓸모 있는 것 없음 |
| abstract unix 소켓 연결 | 거부 또는 쓸모 있는 것 없음 |
| 에이전트 worktree 안 / 밖에 쓰기 | 허용 / 거부 |
| 제공자 API 연결 | 허용(OD2에 따라) |
| 자기 소켓으로 mailbox MCP 호출 | 동작 |
| fork bomb, 메모리 과다 사용 | `TasksMax` / `MemoryMax`로 막히고 노드는 응답을 유지 |

## 단계

- **O1** sandbox만: 에이전트 사용자, unit 템플릿, sudo 규칙, probe 스크립트, 열린 턴의 메모리 수치. 아직 어느 에이전트도
  열려서 돌지 않는다.
- **O2** driver: 열린 에이전트의 턴을 unit에서 돌린다. `tools` 필드, 등록과 relay 검사, 에이전트별 자격 증명, 소켓 소유.
- **O3** worktree를 쓰는 task: snapshot에서 worktree, `task_update`로 patch 반환, 허브에서 사람 승인 작업으로 patch
  기록(`auto:engine` 없음).
- **O4** 허브 sandbox와 `node-1`에서 시험: 열린 claude, agy, codex 하나씩. `llm.env`, 다른 호스트로 ssh, `sudo`를
  요구하는 주입 메일이 모두 실패하고 기록에 보여야 한다.

## 운영자 결정

여섯 가지 모두 2026-11-15 운영자가 권장안대로 정했다.

| # | 질문 | 결정 (2026-11-15) |
|---|---|---|
| OD1 | sandbox 방식 | 전용 사용자와 systemd transient unit(새 패키지 없음); 대안: AppArmor profile을 둔 bwrap, LXD 컨테이너 |
| OD2 | 열린 에이전트의 인터넷 나가기 | 인터넷은 열고 사설 대역과 tailnet은 막음; 필요하면 나중에 허용 목록 proxy |
| OD3 | 자격 증명 | 열린 에이전트마다 자기 홈에 폐기 가능한 자격 증명 하나; 노드 사용자 것은 쓰지 않음 |
| OD4 | 쓰기가 닿는 곳 | 허브에 사람 승인 작업으로 기록되는 patch; Gitea로 push하지 않고 자동 적용도 없음 |
| OD5 | 노드 | `node-1` 먼저, 나머지는 O4 뒤 |
| OD6 | "전부"에 push, 배포, fleet 작업이 들어가는가 | 아니다: 그것은 허브와 사람에게 남는다(상시 결정) |

## 위험

| 위험 | 대응 |
|---|---|
| 메일이 열린 셸을 노드의 키나 다른 호스트로 몰아감 | 별도 사용자, 가려진 홈, 네트워크 거부 목록, O1의 probe와 O4의 주입 시험 |
| 에이전트 자신의 자격 증명이 메일로 새어 나감 | 에이전트별 자격 증명(OD3), 노드 사용자를 건드리지 않고 폐기 |
| unit 시작용 sudo 규칙이 권한 경로가 됨 | 규칙은 unit 템플릿 하나와 고정 인자만 허용; 에이전트 사용자에게는 sudo가 전혀 없음 |
| 열린 턴이 노드 자원을 다 씀 | `MemoryMax`, `TasksMax`, `CPUQuota`, `RuntimeMaxSec`; 상주 슬롯 계산은 그대로 |
| 열린 에이전트의 patch에 해로운 변경이 들어 있음 | 모든 task patch는 사람 승인; `auto:engine` 경로 없음 |
| 업데이트로 sandbox가 지워지거나 깨짐 | inventory의 probe 결과; 열린 에이전트는 `down`, 노드 사용자로는 돌지 않음 |

## 구현 메모

O1 (2026-11-18, 허브에는 2026-11-19 `b3c09d2`로 배포; `node-1`에 설치): `deploy/sandbox/` (래퍼
`/usr/local/sbin/guardian-sandbox-run`, sudoers 규칙, `limits.conf`), `scripts/install-sandbox.sh`와
`scripts/lib/sandbox-remote-install.sh`, `scripts/probe-sandbox.sh`와 `scripts/lib/sandbox-probe-remote.sh`,
`scripts/lib/sandbox-probe-inner.py`, 판정기 `cli/llm-agent/sandbox_probe.py`. 설계와 다른 점:

- **unit 템플릿 파일이 없음.** sudo 규칙은 root 래퍼 하나만 허용하고, 래퍼가 인자(에이전트 이름, `limits.conf`의
  상한, `TIMEOUT_MAX` 이하의 `--timeout`)를 검사한 뒤 `systemd-run` 일시 unit을 직접 만든다. 고정 속성 목록은 설치
  스크립트가 해시로 버전을 매기는(`sandbox_version`) 스크립트 하나에 있다.
- **loopback도 막음.** `IPAddressDeny=`에 `127.0.0.0/8`과 `::1`도 넣고, 리졸버 스텁(`127.0.0.53`, `127.0.0.54`)만
  허용한다. 그러지 않으면 노드 loopback에서 듣는 서비스(예: `node-1`의 postgres)에 턴이 닿는다. 거부 대역은 패킷을
  버리므로 거부된 연결은 timeout까지 기다린다.
- **가린 경로.** `ProtectHome=tmpfs`는 `/root`를 비어 있지만 목록을 볼 수 있는 tmpfs로 남기고 `/run` 아래는 하나도
  가리지 않는다. 그래서 `InaccessiblePaths=`에 `/root`와 node-1 probe가 찾은, 누구나 연결할 수 있는 소켓(tailscale,
  dbus, snapd, lxd, rpcbind, docker, containerd, postgresql, `systemd/notify`, `systemd/io.systemd.ManagedOOM`)을
  적는다. 허용 목록이 아니므로 노드에 새로 생긴 공개 소켓은 probe 실패로 드러난다. 노드에 열린 에이전트를 허용하기
  전과 업데이트 뒤에 probe를 다시 돌린다. 추상(abstract) unix 소켓은 이 방법으로 가릴 수 없어서, probe는 연결된
  추상 소켓을 실패가 아닌 경고로 보고한다.
- **inventory 키.** 판정은 `worker_install` 아래가 아니라 항목의 최상위 `resident_sandbox`(버전, 상한, 판정, 실패
  목록, 메모리 수치)에 저장한다. `scripts/install-worker.sh`가 `worker_install` 객체를 통째로 바꾸기 때문이다.
  probe 스크립트는 `scripts/probe-sandbox.sh <node> [--record]`이고, `scripts/probe-resident.sh --open <node>`가
  여기로 넘긴다.
- **probe 세부.** 스트레스 상한은 실제로 관찰됐을 때만 인정한다(메모리 소모 프로세스가 죽음, fork가 `TasksMax`에
  막힘). TCP 대상(허브, 다른 노드, 노드 자신의 주소와 모든 listen 포트)은 병렬로 확인하고 줄마다 출력을 flush한다.
  버려지는 연결 백여 개를 차례로 기다리면 unit의 `RuntimeMaxSec`를 넘었기 때문이다. 워크로드 단계는 몇 분 동안
  출력이 없으므로 probe의 ssh는 keepalive(`ServerAliveInterval`)를 쓴다.
- **워크로드.** 메모리 수치는 저장소의 Python 테스트를 unit 안에서 `scripts/ci.sh`처럼(CI 환경, 격리된 데이터
  디렉터리, `GUARDIAN_LLM_PROVIDERS=none`) 돌려서 잰다. `git archive HEAD`에서 보내며, 테스트가 읽으므로
  `scripts`, `configs`, `tools`, `deploy`, `apps/agent`도 함께 보낸다. `test_exaone_provider`는 뺀다. loopback HTTP
  서버를 띄우는데 sandbox가 설계상 이를 막기 때문이다. 워크로드 종료 코드는 보여 주기만 하고 판정에 쓰지 않는다
  (시간에 민감한 테스트는 `CPUQuota` 아래에서 실패할 수 있다).
- **`node-1` 결과(2026-11-18, sandbox `2747b475ef4a`): 통과**, 실패 0건, 경고 1건(추상 소켓
  `@ISCSIADM_ABSTRACT_NAMESPACE`에 연결됨; 가리려면 network namespace가 필요). sudo 거부; 노드 사용자의 `llm.env`,
  `.ssh`, `.claude`, `.codex`, `.config`, `worker.sock`, 다른 에이전트의 홈과 소켓은 가려짐; `/etc/sudoers.d` 거부;
  자기 홈과 소켓 디렉터리는 쓰기 가능, `/tmp`와 `/var/tmp`는 전용; DNS와 두 provider API에 닿음; TCP 대상 111개 모두
  timeout; 다른 프로세스는 보이지 않음; user unit과 crontab 없음; 띄운 데몬은 턴이 끝나자 사라짐; 메모리 소모
  프로세스는 죽고 fork는 63개에서 멈춤(`EAGAIN`). 앞선 실행에서 `/root` 목록 열람과 postgres, `systemd/notify`,
  `ManagedOOM` 연결이 발견되어 이번 실행 전에 `InaccessiblePaths=`에 넣었다.
- **열린 턴의 잠정 메모리 수치:** 워크로드(Python 테스트 전체, 307초)의 피크는 두 번의 실행에서 `MemoryMax=2G`에 대해 157-158 MiB였다.
  `limits.conf`는 O4에서 빌드와 테스트를 하는 실제 CLI 턴을 잴 때까지 2G, 작업 512개, CPU 1개, 3600초를 유지한다.
  시간에 민감한 테스트 하나가 `CPUQuota` 아래에서 실패한다(워크로드는 판정하지 않음).

O2 (2026-11-19, 허브에는 2026-11-19 `b3c09d2`로 배포, 중계 재시작 뒤 닫힌 에이전트 둘 다 epoch 3으로 복구; 노드에는 아직 아무것도 설치하지 않음, 열린 에이전트 없음): `cli/llm-agent/resident_open.py` (노드 쪽:
capability, 턴 전 검사, 턴 명령, launcher 입력, MCP 사본), `cli/llm-agent/sandbox_info.py` (허브가 계산하는 sandbox
버전), launcher `deploy/sandbox/guardian-sandbox-launch.py`, 래퍼의 `--stop`, `scripts/lib/sandbox-remote-install.sh`의
CLI 사본과 launcher, 자격 증명 스크립트 `scripts/sandbox-credential.sh`, `--resident-add`의 `--tools open`,
`mail_relay.py`의 relay 보류, 열린 카드 절. O2에서 여는 것은 claude뿐이다(운영자, 2026-11-19). 코드는 agy도 받는다.
설계와 공통 계약에서 달라진 점:

- **sandbox 버전.** 버전은 래퍼, `limits.conf`, sudoers 템플릿, launcher를 이 순서로 이어 붙인 sha256의 앞 12자리
  16진수다. 허브(`sandbox_info.sandbox_version()`)와 설치 스크립트가 같은 값을 계산한다. launcher가 해시에 들어갔으므로
  `2747b475ef4a`에 대한 O1 판정은 낡았다. 지금 트리는 `9be6d2ccaabc`다.
- **capability는 hello로 온다.** 노드 데몬은 `open_tools`와 `sandbox_version`을 알린다. `open_tools`는 래퍼와
  launcher가 root 소유 일반 파일이고 `installed.json`이 읽힐 때만 참이다. relay는 agents 프레임을 만들 때마다 이를
  inventory 키 `resident_sandbox`와 이 릴리스의 버전에 대어 본다. capability가 없는 노드의 열린 에이전트는 프레임에서
  빠지고 `node_without_open_tools`로 down이 된다. 버전이 다르면 spec에 `blocked`를 실어 보내고 에이전트는
  `sandbox_version_mismatch`로 down이 된다.
- **허브가 보류한 에이전트를 붙잡아 둔다.** 프레임에서 빼거나 막는 것에 더해, relay는 그런 에이전트의 `bind_begin`과
  `admit`을 오류 `agent_held`(detail에 사유)로 거부한다. 옛 spec을 들고 있는 데몬도 다시 bind할 수 없다.
- **턴마다 노드에서 검사.** 열린 턴 전마다 driver는 `open_problem()`을 돌린다. 검사 순서는 다음과 같다:
  `blocked`, provider, 이름, 래퍼와 launcher, `installed.json`, 버전, 그 안의 에이전트와 provider CLI 사본, sock
  디렉터리, 데몬의 그룹 소속. 이어서 `sudo -n guardian-sandbox-run --stop <agent>`를 돌려, 잃어버린 턴이 남긴 unit이
  새 턴 옆에서 돌지 못하게 한다. 하나라도 실패하면 프로세스를 띄우지 않고 `agent_exit`로 알린다. 열린 spec은 닫힌
  경로로 넘어가지 않는다. sandbox에 bind된 에이전트의 spec이 closed로 바뀌면 턴을 `open_spec_changed`로 거부하고,
  다음 epoch에 다시 bind하게 한다.
- **CLI를 바로 띄우지 않고 launcher를 거친다.** 턴은 `sudo -n guardian-sandbox-run --timeout <턴+30> --chdir
  /home/gsa-<name>/work <name> -- /usr/bin/python3 -I /usr/local/lib/guardian-sandbox/launch.py`로 돈다. 데몬은
  stdin에 JSON 객체 하나(provider, version, argv, files, stdin)를 쓴다. unit 안에서 root 소유 launcher는 다음을 한다:
  - 입력을 검사한다;
  - 턴의 파일을 쓴다(0600, 디렉터리 0700);
  - 정해진 키 목록으로 환경을 새로 만든다;
  - claude 토큰을 `~/.guardian/credentials/claude-oauth-token`에서 읽어 환경에만 넣는다;
  - root 소유 CLI 사본을 exec한다.

  launcher는 잘못된 입력에 2, `cli_missing`이나 `no_credential`에 3으로 끝나고, driver는 3을
  `sandbox_<사유>`로 알린다. 열린 claude는 `--permission-mode bypassPermissions`, 에이전트 MCP 사본의 mailbox와 repo
  MCP 서버, `--strict-mcp-config`로 돌고 `--tools` 제한은 없다.
- **CLI 사본.** `scripts/install-sandbox.sh`는 `configs/resident-providers.json`에서 상주 버전을 읽고
  `readlink -f ~node/.local/bin/<provider>`를 `/usr/local/lib/guardian-sandbox/cli/<provider>-<version>/`(root 소유,
  0755)에 복사한다. 복사 전에 `timeout 120 ... --version`이 그 버전을 출력해야 한다. 복사 실패는 경고로 끝나고
  `installed.json`의 `cli`에서 빠진다. `--no-cli`는 복사를 건너뛴다. `--remove`는 `/usr/local/lib/guardian-sandbox`를
  지운다.
- **자격 증명.** `scripts/sandbox-credential.sh <node> claude`는 토큰을 stdin에서 읽고(TTY는 거부) ssh stdin으로만
  보낸다. argv, 환경 변수, 임시 파일, 로그에는 남기지 않는다. 노드에서는 에이전트 사용자가 umask 077과 rename으로
  쓴다. 스크립트 출력은 `credential written for <agent>`뿐이다. `--remove`는 노드의 토큰을 지운다. Anthropic 쪽 토큰
  폐기는 운영자 몫이다.
- **소켓 디렉터리는 데몬 소유.** 설계는 소켓 디렉터리를 에이전트에게 주었다. 대신
  `/var/lib/guardian-sandbox/sock/<name>`은 노드 사용자 소유, 그룹 `gsa-<name>`, 모드 2750이다:
  - 에이전트는 들어가서 소켓에 연결할 수 있지만 항목을 만들거나 이름을 바꾸거나 바꿔치기할 수 없다. 그래서 데몬이
    bind하는 자리에 symlink를 심을 수 없다;
  - 데몬은 umask 0117로 `e<epoch>.sock`을 bind한다(0660, 그룹은 setgid 디렉터리에서);
  - MCP 사본은 `mcp/<worker 버전>/`에 둔다(파일 0640, 디렉터리 2750);
  - 정리는 lstat 뒤에 unlink하고, 링크를 따라가거나 rmtree하지 않는다.

  설치 스크립트는 O1에서 만든 디렉터리의 소유자를 다시 바꾼다. probe의 `write.own_sock_dir`은 이제 거부되는 것이
  정상이고, probe의 stub listener는 `sg` 없이 노드 사용자로 bind한다.
- **그룹 소속은 재시작해야 반영된다.** 설치 스크립트가 노드 사용자를 `gsa-<name>`에 넣는다. 데몬은
  `guardian-worker`가 재시작해야 새 그룹을 얻으므로, 그때까지는 턴 전 검사 `sandbox_group`이 에이전트를 붙잡는다.
- **등록.** `--resident-add ... --tools open`의 조건은 다음과 같다:
  - claude나 agy(codex는 `resident: false` 그대로);
  - `^[a-z][a-z0-9]{0,15}$`에 맞는 이름;
  - 정확히 이 릴리스 버전에서 통과한 `resident_sandbox` 판정;
  - 노드 상주 예산 안의 `resident_open_memory_mib`(2048, O1의 `MemoryMax`). `configs/worker-requirements.json`과
    `worker_probe`의 `REQUIREMENT_KEYS`에 새로 들어갔다.

  닫힌 선언은 이 키를 쓰지 않아 바이트 단위로 그대로다.
- **worker 파일.** `resident_open.py`를 `deploy/worker/files.txt`와 worker 설치 스크립트 목록에 넣었다.
- **카드.** 열린 에이전트의 시작 카드에는 8절 "Open tools"가 있고 버전은 4다. 닫힌 카드는 버전 3, 바이트 단위로
  그대로다(`golden/resident_cards.txt`; 열린 카드는 `golden/resident_cards_open.txt`가 고정).
- **probe 추가.** probe에 검사가 두 개 늘었다:
  - 유효한 claude 입력에 자격 증명이 없는 launcher 실행은 3으로 끝나야 한다(`launcher.exit`, `launcher.result`);
  - `--stop`은 돌고 있는 `sleep 60` unit을 10초 안에 끝내야 한다(`stop.started`, `stop.exit`, `stop.active`).
    `stop.started=0`은 증명된 것이 없으므로 실패다.

O3 노드 쪽(2026-11-19, 허브에는 2026-11-19 `02d9b26`로 배포, `node-1` worker도 그 커밋으로 재설치; 허브 쪽은 아래에 있다): `resident_node.py`의 `task_worktree`(repo 도구처럼 노드에서 답하고 허브로 절대 넘기지 않는다;
`not_open`, `bad_input`, `not_owner`, `task_released`, `snapshot_pending`, `no_snapshot`, `bundle_failed`),
`resident_open.py`의 번들 파일 담당(`write_task_bundle` / `remove_task_bundle` / `task_bundle_path`:
`tasks/` 2750, 파일 0640, 임시 이름 + fsync + rename; 커밋은 번들 헤더에서 다시 읽고 멱등 검사에 git은 돌지
않는다), `resident_tasks.owned_entry`의 소유 검사, 작업이 테이블을 떠날 때마다(update, release, `tasks` 프레임,
`clear_sock_dir`을 통한 은퇴) 번들을 지우는 테이블의 drop 콜백, `mailbox_mcp.py`의 MCP 쪽(`--worktrees`: 도구,
턴 사이에 보존되는 `$HOME/work/tasks/<task>`로의 clone, 256 KiB 한도가 붙은 `git diff --cached --binary
--no-renames <base>` 수집, 승인된 update나 release 뒤의 worktree 제거; 닫힌 도구 목록과 그 이름들은 바이트 그대로
유지). 이탈과 메모:

- **같은 스냅숏 키는 다시 번들하지 않는다.** 이미 그 작업의 커밋을 `refs/heads/snapshot` 헤더 줄에 담은 번들은
  그대로 돌려주고, 다른 커밋의 번들은 다시 쓴다.
- **task_update의 프레임 예산은 패치를 센다.** `_attach_calls`는 패치를 포함해 프레임 전체를 재므로 예산을 넘은
  프레임은 호출 로그부터 버린다; 패치는 절대 버리지 않는다.
- **반복된 task_worktree는 작업물 위로 clone하지 않는다.** `$HOME/work/tasks/<task>`가 있으면 기록된 base를
  답하고 clone은 그대로 둔다; base 파일이 없는 worktree는 에러고, 에이전트가 고치려면 디렉터리를 지우고 다시
  clone하는 수밖에 없다.
- **승인된 task_update는 상태와 무관하게 worktree를 지운다**(failed도 같다): 작업이 끝났으므로 다음 claim은 새
  clone에서 시작한다. 거부된 update(bad_patch, patch_too_large, 전송 실패)는 유지한다.
- **카드.** 열린 카드 버전 5는 8번 절에 worktree 흐름을 더했다; 닫힌 카드는 바이트 그대로다
  (`golden/resident_cards.txt`는 그대로, `golden/resident_cards_open.txt`는 다시 만들었다).
- **허용 도구 목록에 도구를 더하지 않았다.** 열린 claude 턴은 `bypassPermissions`에 `--allowedTools` 없이
  돌고, 열린 agy 설정에는 권한 목록이 없어서 `mcp__mailbox__task_worktree`가 들어갈 목록 자체가 없다.

O3 허브 쪽(2026-11-19, 2026-11-19 `02d9b26`로 배포): `task_patch.py`가 패치를 apply_patch 편집으로 바꾸고
`mail_relay._tool_task_update`가 그것을 액션 하나로 기록한다. 세부와 이탈:

- **패치는 workspace 객체 저장소에서 작업의 `sha`에 대고 적용한다.** 임시 index(0700 임시 디렉터리 안의
  `GIT_INDEX_FILE`, `read-tree <sha>`, `git apply --cached`, 내용은 `git show :<path>`로 다시 읽음)를 쓰므로
  workspace의 작업 트리, HEAD, index, worktree 목록은 건드리지 않는다. 허브는 업로드 뒤 스냅숏 저장소를 남기지
  않으므로 `patch_base`는 40자리 16진수인지만 보고 행에 기록하며, 어떤 커밋과도 비교하지 않는다.
- **거부는 경로를 밝힌다**: 삭제, 이름 바꾸기와 복사, 모드 변경, 심볼릭 링크, 서브모듈, 바이너리, UTF-8이 아닌
  텍스트, `SnapshotEnsurer.allowed_paths(project, sha)` 밖의 수정 경로, `sha`에 이미 있는 추가 경로(스냅숏이 뺀
  파일을 덮어쓸 수 없다), `.git`이나 `..` 성분, 비밀 경로(`code_context.is_secret`), `patch_ops` 한도
  (`patch_too_large`가 한도를 밝힌다). 바뀐 구간마다 편집 하나가 되고, 적용 시점에 `old`가 유일해질 때까지
  문맥 줄로 넓힌다. `patch_ops.plan`이 적용 후 내용을 정확히 재현해야 하고, 그다음 프로젝트의 `MCPClient`가
  편집을 `jev_mcp:apply_patch`로 검증한다.
- **모든 거부는 `store.task_update` 전에 답한다.** 그래서 작업은 `in_progress`로 남고 에이전트가 고쳐 다시
  update할 수 있다(닫힌 에이전트, 프로젝트 없는 작업, `completed`가 아닌 상태는 `patch_not_allowed`).
- **멱등성의 열쇠는 op-id 재생이 아니라 작업 행이다**(재생은 액션이 생기기 전에 캐시된 답을 돌려준다):
  `patch_action`이 있는 행은 그 id를 돌려주고, 이미 PROPOSED apply_patch 액션을 가진 스레드(`add_action`과
  행 갱신 사이의 장애)는 그 고아 액션을 이어받는다. 스레드는 `thread_task_<작업 id의 -를 _로>`, 라운드 0,
  `base_sha`는 작업의 sha다.
- **두 DB 사이의 장애 구간**(store 행은 쓰고 액션은 아직 없음)은 relay 프로세스마다 첫 세션 전에 한 번 도는
  `reconcile_task_patches`가 닫는다. 저장된 패치를 다시 변환해(그때는 데워진 스냅숏 ensurer가 없으므로 sha의
  `RepoTools` 읽기 가능 경로 집합을 쓰고, `patch_base` 자리에는 행의 sha를 쓴다) 빠진 액션을 만든다. 이제 변환이
  실패하면 `task_patch_failed`로 남기고 액션 없이 둔다. `--tasks`는 `action #N`이나 `action pending`을 보인다.
- **store 스키마 4**는 `tasks.patch`와 `tasks.patch_action`을 더하고(스키마 3 store에는 `ALTER TABLE ADD
  COLUMN`), 이벤트 `task_patch` {task, action, files, bytes}를 더한다.
- **자동 경로는 구조적으로 없다**: 스레드가 `thread_task_`로 시작하는 액션은 `GUARDIAN_AUTO_APPLY`와 무관하게
  `action_ops.is_auto_approvable`이 False를 돌려주고 `auto_run_actions`가 건너뛴다(규칙 5, OD4). `--actions`,
  `--run-action`, 대시보드 액션 목록은 스레드 뒤에 토론 작업이 없어도 된다.
- **종단 간 테스트** `test_relay_e2e.OpenAgentPatchEndToEnd`: 임시 sandbox 안의 열린 에이전트 소켓, 실제 데몬과
  번들을 거친 `task_worktree`, clone, 수정 하나와 추가 파일 하나, 버려지는 위조 `patch` 인자, 실제 `MCPClient`로
  변환한 수집 diff, 정확히 하나의 PROPOSED 액션, 지워진 worktree와 번들, 그대로인 workspace.

노드에서 처음으로 실제 열린 턴이 돌아간 뒤의 O1-O3 수정(2026-11-19, 2026-11-19 `bad2572`로 배포: 허브, `node-1`
worker, sandbox 재설치(드롭인이 바뀌었으므로 예상대로 노드 사용자의 user manager를 재시작했다), sandbox 버전
`9be6d2ccaabc`에서 probe 실패 0, 경고 1로 기록; O1 probe는 ssh 셸에서 돌았으므로
이것들은 거기서 보이지 않았다; 에이전트를 돌리려고 결함 1-4는 먼저 `node-1`에서 손으로 고쳤다):

- **work 디렉터리(결함 1).** systemd가 `--chdir`을 런처 시작 전에 해석하므로 `/home/gsa-<name>/work`이 없으면
  턴이 status 200/CHDIR으로 죽었다(런처의 ensure_dir은 너무 늦는다). `scripts/lib/sandbox-remote-install.sh`의
  `ensure_agent`가 이제 디렉터리를 남긴다(에이전트:에이전트, 0700, `install -d`; 이미 있으면 그대로 두고 심볼릭
  링크나 디렉터리가 아닌 것은 거부). 런처의 ensure_dir은 그대로다.
- **worker 드롭인(결함 2).** `guardian-worker.service`의 `NoNewPrivileges=yes` 때문에 데몬의 setuid
  `sudo -n guardian-sandbox-run`이 실패했다(`sandbox_stop_failed`). 기본 유닛은 그대로(닫힌 노드는 강화 유지);
  `deploy/worker/sandbox-sudo.conf`가 정확히 `NoNewPrivileges=no`만 정하고 `scripts/install-sandbox.sh`가 함께
  실어 보낸다(sandbox 버전 해시에는 들어가지 않는다, 해시는 test_sandbox_wrapper.py의 네 파일 그대로). 설치는
  노드 사용자의 `~/.config/systemd/user/guardian-worker.service.d/`에 쓴다(디렉터리는 만들 때만 0755, 파일 0644,
  temp 이름 + mv, 홈 아래 경로 구성요소가 심볼릭 링크면 거부); 통째 `--remove`는 지우고(`.service.d` 디렉터리는
  그때 비어 있을 때만), `--remove --agent`는 남긴다.
- **묵은 그룹(결함 3).** 동작 중인 `user@<uid>.service`는 시작할 때의 보조 그룹을 유지하므로 `usermod -aG` 뒤에도
  `open_problem`이 `sandbox_group`을 보고했다. `do_install` 끝(그리고 통째 `--remove` 뒤, 드롭인 때문에)에서 설치가
  매니저의 `Groups:` 줄을 읽고, gsa gid가 빠졌거나 드롭인이 새로 쓰였거나 바뀌었으면 `user@<uid>.service`를
  재시작한다(그 계정의 모든 사용자 서비스가 함께 재시작된다는 경고를 함께 출력; enable된 서비스는 다시 뜨고 진행
  중인 resident 턴은 끊기지만 드라이버가 다시 수행한다). 그 외에는 드롭인이 바뀐 경우에만 daemon-reload; 비활성
  매니저는 아무 것도 할 필요 없다. `--no-restart`(두 스크립트 모두)는 재시작을 건너뛰고 명령을 그대로 출력한다.
- **probe.** `scripts/probe-sandbox.sh`가 데몬 자신의 시점을 추가로 확인한다: 모든 gsa 에이전트의 work 디렉터리(있고,
  디렉터리이고, 에이전트 소유)와 `guardian-worker.service`가 돌고 있을 때 그 `NoNewPrivs: 0`과 노드 사용자의 모든 gsa
  gid가 `Groups`에 있는지(`daemon.workdir.*`, `daemon.worker*`; 멈춘 worker는 경고). probe의 cleanup은 이제
  `/home/gsa-probe/work`를 지우지 않고 비운다(디렉터리는 설치 것).
- **소켓 디렉터리 그룹(결함 4).** `install_mcp_copy`와 `write_task_bundle`은 에이전트 소켓 디렉터리의 setgid
  비트에 기대어 새 항목이 `gsa-<name>` 그룹을 받게 했다. 데몬이 한때 그 그룹의 구성원이 아닌 채로 돌았고(결함 3),
  리눅스는 구성원이 아닌 프로세스가 chmod하면 `S_ISGID`를 지우므로 `mcp/`가 데몬 자신의 그룹으로 0750이 되었고,
  이후 항목이 모두 그 그룹을 물려받아 에이전트의 mailbox·repo MCP 서버가 `CONNECTION_CLOSED`로 실패했다.
  `_ensure_dir`은 모드만 봤으므로 아무것도 이를 고치지 않았다. 이제 `resident_open.py`가 에이전트 gid를 구하고
  (그룹이 없으면 `OSError`로 거부, 호출자는 이미 이를 거부된 bind나 잃은 턴으로 바꾼다), 쓰는 모든 디렉터리와
  파일에 그룹을 직접 정하고(같은 fd에서 fchown 다음 fchmod; chown이 `S_ISGID`를 지울 수 있으므로 모드가 뒤),
  gid와 모드를 다시 확인하며, 기존 MCP 사본이나 다시 쓰는 작업 번들의 그룹이 틀리면 건너뛰지 않고 고친다.
  번들 temp 파일은 rename 전에 chown하고, 설치기의 드롭인 temp 이름은 쓰기 전에 지운다(그 디렉터리는 노드
  사용자 소유이므로).
- **턴 예산.** `mail_store.py`의 시간당 12턴은 받아들인 모든 턴을, 잃은 턴까지 센다. 그래서 위 결함들(CLI가 돌기
  전에 잃은 턴)이 `coder`의 예산을 써 버렸고, 드라이버는 시간이 넘어갈 때까지 600초씩 물러났다. 바꾸지 않았고
  후속 과제로 적어 둔다(턴 전 거부는 예산에서 뺄 수 있다).
- **테스트.** `cli/llm-agent/test_sandbox_installer.py`가 드롭인의 정확한 내용, work 디렉터리 단계, 두 스크립트의
  `--no-restart` 파싱, 드롭인 배송, `deploy/sandbox`가 여전히 해시되는 네 파일만 담는 것을 못 박는다;
  `test_resident_open.py`가 그룹 복구(디렉터리, MCP 파일, 다시 쓰는 번들, 그룹 없음, 먹히지 않은 chown)를 다룬다.

에이전트를 열기 전 운영자 단계(2026-11-19 새 claude 에이전트 `coder@node-1`에 대해 모두 마침;
`claude@node-1`과 `agy@node-1`은 닫힌 채):

1. 허브를 배포한다.
2. `scripts/install-sandbox.sh node-1`(새 sandbox 버전, launcher, claude 사본, sock 디렉터리 소유)을 돌리고
   `scripts/probe-sandbox.sh node-1 --record`를 돌린다.
3. `node-1` worker를 다시 설치해 `resident_open.py`를 싣고 새 그룹으로 재시작하게 한다.
4. 워크스테이션에서 `claude setup-token`을 돌려 그 출력을 `scripts/sandbox-credential.sh node-1 claude`에 파이프로
   넘긴다.
5. 새 이름이면 `--resident-add <name> --node node-1 --provider claude --tools open`을 돌린다. 이미 선언된
   에이전트를 열려면 먼저 `--resident-remove`로 지운다. `--resident-add`는 이미 선언된 이름을 거부한다. 그때까지
   `claude@node-1`과 `agy@node-1`은 닫힌 채로 남는다.

첫 실제 작업(2026-11-19, `bad2572` 위): 운영자가 `server-guardian` 프로젝트 `02d9b26`에서 `coder@node-1`에게
`test: action_reason skips leading blank lines and truncates` 작업을 만들었다. 턴 예산의 백오프(위 참고)가 끝난 뒤
첫 허용 턴이 29초 돌아 `normal`로 끝났고, 작업을 claim하고 완료했으며, 허브에 정확히 하나의 PROPOSED
`jev_mcp:apply_patch` 액션을 남겼다(thread `thread_task_<task>`, `base_sha` `02d9b26`, `decided_by` 비어 있음:
jev 게이트 없음, `auto:engine` 없음). 패치는 `test_task_patch.py`에 테스트 하나를 더하고 다른 파일은 건드리지
않는다; 그 커밋의 scratch 사본에 적용하면 모듈의 24개 테스트가 통과한다. 작업 worktree와 번들은 뒤에
`/home/gsa-coder/work/tasks`에서 사라져 있었다. 액션은 운영자 승인을 기다린다; 시험을 돌린 세션은 아무것도
승인하거나 적용하지 않았다.

설치기가 기존 에이전트를 유지(2026-11-19, `293c446` 배포 뒤): 그 배포의 sandbox 재설치를 `--agent coder` 없이
실행했고, `do_install`은 `probe`, `probe2`와 그 실행에 준 이름만 `installed.json`에 썼다. `gsa-coder` 사용자는
남았지만 `open_problem`이 `--agent coder`로 재설치할 때까지 coder의 모든 턴을 `sandbox_agent_missing`으로
거부했다(epoch 11~21). 이제 원격 설치기는 기존 sandbox 에이전트(GECOS가 `guardian sandbox agent`로 시작하는
`gsa-*` 사용자, 전체 `--remove`와 같은 선택)를 모아 `probe`, `probe2`, 모든 `--agent`에 더하고, 그 합집합에
`ensure_agent`를 돌려 합집합을 쓰며, 유지한 기존 에이전트를 알린다; `--agent`는 더하기만 한다.
`--remove --agent NAME`도 이제 `installed.json`의 `agents`를 NAME 없이 다시 쓰고, `version`, `installed`,
`cli`는 그대로 둔다. 해시되는 네 파일은 바뀌지 않으므로 sandbox 버전은 `109bff91ace1` 그대로다.

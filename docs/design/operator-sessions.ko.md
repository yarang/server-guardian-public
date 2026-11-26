# 운영자 세션: 허브를 거친 대화형 세션 원격 제어 (설계)

[English](operator-sessions.md) | 한국어

상태: 2026-11-17 제안. 결정 SD1-SD8과 후속 결정 SD3a, SD6a를 운영자가 2026-11-20에 내렸다("결정" 참고). 같은 날
S0 2부 뒤 운영자가 SD1과 SD2를 고쳤다: 허브가 워크스테이션에 접속하는 것이 아니라 워크스테이션이 허브로 연결을 연다.
같은 날 S0 3부 뒤 SD9(연결 키마다 `from=`), SD10(허브의 `authorized_keys2`를 Guardian이 맡음), SD11(원격 승인은 사람이 켠
세션에서만, SD5를 좁힘)을 정했다. 같은 날 SD12(데몬이 접속할 허브 주소: tailnet IPv4, 그다음 DNS 이름)를 정했다. 만든 것 없음. 다음: 프로브 S0의 남은 부분. 다이어그램: [operator-sessions-architecture.mmd](operator-sessions-architecture.mmd). 본뜨는 제품
기능에 대한 참고 노트:
[../analysis/2026-11-17-remote-control-reference/notes.ko.md](../analysis/2026-11-17-remote-control-reference/notes.ko.md).

## 목표

운영자의 요청(2026-11-17): Claude Code의 Remote Control과 세션 간 메시징이 하는 일을, 벤더의 중계 서버에 기대지 않고
운영자 자신의 장비들을 위해 Guardian 위에 만든다.

운영자는 여러 워크스테이션(macOS, Windows의 WSL, 네이티브 Windows)에서 **대화형** Claude Code 세션을 쓴다. 각 세션은
사람의 작업 세션이지 상주 에이전트가 아니다. 지금 이 세션들에 닿는 방법은 둘이고, 둘 다 모자란다.

- **Claude Code Remote Control.** 세션이 벤더 서버에 등록되어 다른 세션의 `ListAgents`에 이름으로 보이고,
  `SendMessage`를 받으며, claude.ai나 휴대폰 앱에서 조작할 수 있다. 다만 claude.ai 로그인이 필요하고(API 키, `claude
  setup-token` 토큰, 다른 `ANTHROPIC_BASE_URL`은 안 됨), 연결 중에는 대화 기록이 벤더 서버에 저장되며, 오래 도는 세션은
  로그인이 만료되면 멈추고, 다른 장비로 보낸 메시지가 도착했는지 보낸 쪽에 확인되지 않는다(실제로 다른 장비의 세션에
  보낸 메시지가 두 번 사라진 뒤에야 하나가 도착했다).
- **워크스테이션 간 SSH.** API 키 CLI로도 되지만 사람이 명령을 치는 방식이다. 보낼 이름, 메일함, 접속 상태가 없고,
  열려 있는 세션이 받을 길이 없다.

Guardian에 더하는 것을 한 줄로: **운영자 세션**은 워크스테이션의 대화형 세션이 이름을 갖고 `resident-agents.md`의 허브
메일망에 들어오는 것이다. 그러면 운영자(대시보드, tailnet 위의 휴대폰), 다른 세션, 상주 에이전트가 그 세션을 보고 메일을
쓸 수 있고, 운영자는 그 세션의 권한 확인 창에 답할 수 있다.

## 지금 있는 것, 그리고 맞지 않는 것

| 필요 | Guardian에 이미 있는 것 | 빈 곳 |
|---|---|---|
| 메일함, 봉투, 멱등 전송, 최소 한 번 전달, hop과 빈도 제한 | `resident-agents.md`(M1, M2, 2026-11-15 `d72baff`로 배포): `mail_store.py`, `mail_relay.py`, 메일함 MCP | 주소가 Tier-1 노드의 에이전트에만 있다 |
| 장비까지의 전송로 | 워커 프로토콜 3: 허브가 tailnet 위로 노드마다 SSH 스트림 하나를 연다 | 워크스테이션은 fleet 호스트가 아니고, 아직 tailnet에 없으며, 잠들 수 있고, 기본으로 SSH 서버가 없다(필요도 없다. 스트림을 스스로 연다) |
| tailnet 가입 | mesh join(`POST /api/mesh/join`, `docs/design/mesh-join.md`) | 워크스테이션 태그가 없고, 워크스테이션이 허브의 SSH 포트에만 닿게 하는 규칙이 없다 |
| 쉬고 있는 CLI 깨우기 | 상주 `Stop` 훅 이어가기와 고정 깨움 문장(M0, M2d) | 대화형 세션은 키보드 앞에 사람이 있다. 깨우기가 사람과 부딪쳐서는 안 된다 |
| 봉투가 아니라 소켓에서 정하는 신원 | 데몬이 에이전트와 epoch별로 호출에 도장을 찍는다 | 워크스테이션에는 워커 데몬이 없고, 허브는 어떤 키도 워크스테이션에 묶지 않는다 |
| 대시보드와 인증 | `AGENT_TOKEN`을 쓰는 Go API, tailnet 전용 | 세션 화면, 원격 입력, 원격 승인, 세션 요약이 없다 |

상주 에이전트는 허브가 띄우고 감독하는 모델 세션이다. 운영자 세션은 사람이 띄우고, 그 사람의 것이며, 그 사람이 닫으면
끝난다. 허브는 운영자 세션을 띄우지도, 다시 시작하지도, 이어 붙이지도 않는다.

## 제품 기능과의 비교

| Claude Code Remote Control | 이 설계 |
|---|---|
| 세션이 벤더 API에 등록하고 밖으로 나가는 HTTPS로 폴링한다. 들어오는 포트 없음 | 워크스테이션이 tailnet에 가입하고 허브로 나가는 SSH 스트림 하나를 연다. 들어오는 포트도, 워크스테이션의 SSH 서버도 없다(SD1) |
| claude.ai OAuth 로그인만. 거기서 갱신되는 단기 연결 자격 증명 | 워크스테이션마다 그 장비에서 만든 키 하나. 허브는 그 키를 연결 진입점과 워크스테이션 id에 묶는다(`command=` + `restrict`). 워크스테이션에 `AGENT_TOKEN`은 없다. API 키 CLI와 어느 제공자든 된다(SD2) |
| 세션 이름은 `--name`, `/rename` 또는 자동 생성 | 주소 `<name>@<workstation>`, 이름은 세션이 연결할 때 정한다. 워크스테이션 id는 인증에 쓰인 키의 허브 쪽 줄에서 오며 클라이언트가 주장하는 값이 아니다 |
| `ListAgents` 행: idle, busy, offline | `session_list`와 대시보드: `linked`, `busy`, `idle`, `away`, `offline`("상태" 참고) |
| 다른 세션의 메시지는 데이터일 뿐 아무 권한도 주지 않는다 | 같은 규칙, 상주 에이전트와 그대로 |
| 연결 중 대화 기록이 벤더 서버에 저장된다 | 모델이 쓴 턴 요약만 허브에 오고, 도구 출력은 절대 오지 않으며, 7일 보관한다(SD6, SD6a) |
| 앱에서 권한 확인 창 원격 승인 | 기한이 있는 `PermissionRequest` 훅으로 원격 승인. 사람이 켜고 시작한 세션에서만 켜지고, 자동 허용은 없다(SD5, SD11) |
| 휴대폰 앱과 claude.ai | tailnet 위의 대시보드. 인터넷 노출 없음 |

## 구조

```
operator (dashboard over the tailnet, AGENT_TOKEN)
        |  Go API spawns guardian.py --session-* (newCmd)
   hub site-a-arm1: mail store (+ address kind "session") + session and workstation registry
        |  link entry guardian.py --session-link, started by the hub's sshd, one per linked workstation
        ^  hub ~/.ssh/authorized_keys2, owned whole by Guardian, one line per workstation:
        |    from="<tailnet address>",command="<python> <guardian.py> --session-link --workstation <id>",restrict <key>
        |  one SSH stream per linked workstation, opened BY THE WORKSTATION over the tailnet (ssh -T)
   ws-mac-1 / ws-wsl-1 / ws-win-1: session link daemon (stdlib Python, user service)
        own key ~/.guardian/session/link-key, the hub's host key pinned; no SSH server
        dials the hub's tailnet IPv4, then its link name on a connection failure (SD12)
        <- per-session socket or named pipe <- session MCP (stdio child of the interactive CLI)
        <- hooks of the interactive CLI: SessionStart, UserPromptSubmit, Stop, PermissionRequest, SessionEnd
        -> tmux wake (macOS, WSL; operator mail only; never mail content)
```

모양의 규칙:

1. **여전히 별 모양.** 워크스테이션은 서로에게도, fleet 노드에도 닿지 않는다. 모든 메일은 허브를 거친다.
2. **워크스테이션이 자기 연결을 연다(SD1, SD2).** 각 워크스테이션은 기존 mesh join으로, 새 워크스테이션 태그를 단 노드로
   tailnet에 가입한다. 연결 데몬은 워크스테이션의 **자기 키**(`~/.guardian/session/link-key`, 설치기가 만든다. 개인 키는
   워크스테이션을 떠나지 않는다)로 tailnet 위에서 허브의 SSH에 접속한다. 허브의 `~/.ssh/authorized_keys2`(SD10)에서 이 키의 줄은
   `from="<workstation tailnet address>",command="<python> <guardian.py> --session-link --workstation <id>",restrict`이다.
   셸, PTY, 포워딩 없이 연결 진입점만 되고, 그 워크스테이션의 tailnet 주소에서만 통한다(SD9). 허브는 등록할 때 그 줄에
   워크스테이션 id와 주소를 적으므로, id는 인증에 쓰인 키에서 오며 클라이언트에게서 오지 않는다.
   Guardian은 `authorized_keys2`를 통째로 맡는다(SD10): 워크스테이션 등록부에서 워크스테이션마다 `guardian-session:<id>`
   주석을 단 줄 하나씩으로 파일을 임시 파일에 쓰고 제자리로 rename하며, 운영자의 `authorized_keys`는 건드리지 않는다. 워크스테이션은
   `AGENT_TOKEN`도 다른 허브 자격 증명도 갖지 않고, 설치할 때 허브의 호스트 키를 고정한다. 데몬은 설치기가 Settings에서
   가져온 허브의 tailnet IPv4(`--hub-address`)로 접속하고, 그 접속이 실패할 때만 허브의 연결 이름(`--hub-name`, 선택)을
   쓴다. 연결 이름은 운영자가 자기 도메인에 두는 A 레코드로, DNS 전용이며 같은 tailnet IPv4를 가리킨다(SD12). 두 길 모두
   `HostKeyAlias <hub id>`로 접속하므로 고정 항목 하나가 둘을 덮는다. 어느 길에서든 호스트 키가 다르면 접속을 끝내고 알리며,
   다른 길로 다시 시도하지 않는다. 허브의 tailnet IPv4가 바뀌면 설치기를 새 주소로 다시 돌릴 때까지 이름이 연결을 이어 준다.
   Guardian은 DNS를 쓰지 않는다. 연결 진입점은 S0 2부와 3부에서 찾은
   것을 반영한다: 비어 있지 않은 `SSH_ORIGINAL_COMMAND`를 거부하고, 자기 환경을 직접 정하고 절대 경로를 쓰며, 데몬이 다른
   무엇보다 먼저 확인하는 고정된 버전 붙은 인사말을 보낸다. 데몬은 `-T`로 접속하고 진입점은 stdin이 터미널이면 거부한다.
   인사말과 데몬의 응답이 둘 다 통과한 뒤에는 한 워크스테이션의 새 스트림이 이전 스트림을 끝낸다. 그 전에는 진입점이
   저장소도 다른 스트림도 건드리지 않으므로, 끝까지 가지 못한 접속이 멀쩡한 연결을 끊을 수 없다. 허브의 sshd는
   keepalive를 보내지 않으므로 양쪽 끝이 연결 프로토콜 안에서 heartbeat를 보낸다. 연결 방향은 메시지 방향이 아니다: 메일, 깨우기, 승인 요청과 그 답은
   모두 워크스테이션이 연 스트림 위로 양쪽으로 오간다.
3. **워크스테이션에 SSH 서버는 없다.** 연결에는 SSH 클라이언트만 필요하고, macOS, WSL 배포판, Windows에 들어 있다.
   워크스테이션이 허브의 SSH 포트에만 닿게 하는 tailnet ACL은 headscale 정책 변경이다. Guardian은 headscale 정책을 절대 쓰지
   않으므로(headscale에 쓰는 것은 가입과 제거뿐) 운영자가 적용한다. **허브는 필요할 때만 워크스테이션에 닿는다:** 연결을 위해
   워크스테이션에 접속하는 일은 없다. 운영자가 허브를 워크스테이션에 들이고 싶을 때(한 번의 점검 등)는 운영자가 그
   워크스테이션의 SSH 서버를 켜고 `expiry-time=`(맞는 곳에는 `command=`와 `restrict`도)을 단 별도 키를 넣은 뒤, 둘 다 다시
   거둔다. Guardian은 이를 위한 도구를 만들지 않으며 어떤 단계도 이에 기대지 않는다.
4. **잠든 워크스테이션은 오류가 아니라 `offline`.** 장비가 깨어나거나 네트워크가 돌아오면 데몬이 간격을 늘려 가며 다시
   접속한다. 허브는 접속하지 않는다. 메일은 스트림이 돌아오거나 메일이 만료될 때까지 허브에 `queued`로 남는다.
5. **워크스테이션의 권한은 데몬 하나가 쥔다.** 연결 데몬이 스트림을 열어 쥐고, 세션별 스풀, 세션별 소켓(네이티브 Windows에서는 named
   pipe)을 쥐고, 각 호출에 그 호출이 들어온 소켓에 묶인 세션을 도장 찍는다. 워크스테이션에서는 `remote-agents.md`의 워커
   데몬을 대신한다. 자기 모델을 돌리지 않고 CLI도 띄우지 않는다(SD6a의 요약 호출은 세션 훅의 자식으로 돈다, "요약" 참고).
6. **저장소는 상주 메일 저장소.** 세션은 `mail.db`의 새 주소 종류다. 봉투, 종류, 멱등성, hop, 스레드, 크기 제한은 그대로
   다시 쓴다. 작업 보드는 S1-S3에서 세션에 내주지 않는다(열린 문제 1).
7. **호출은 동기식**, 상주 에이전트와 같다. 세션 MCP 호출은 허브의 답을 기다리고(20초), 스트림이 끊겨 있으면 오류를
   돌려준다. 오프라인 보낼편지함은 없다.
8. **사람이 먼저.** 메일은 CLI가 내주는 두 지점(입력 제출, 턴 끝)에서 드러난다. 터미널에 입력되는 것은 "깨우기"의 조건 아래
   SD3의 고정 깨움 문장뿐이다.

### 주소

- `<name>@<workstation>`, 예: `cleaner@ws-mac-1`. `name`은 세션이 연결할 때 정하고(세션 MCP의 `session_link(name)`, 또는
  `GUARDIAN_SESSION_NAME`), 워크스테이션 안에서 고유하다. 쓰이고 있는 이름을 요청한 두 번째 세션은 거부되고 이전 주소를
  유지하거나 주소 없이 남는다.
- `<workstation>`은 스트림이 인증에 쓴 키의 `authorized_keys2` 줄에 허브가 적어 둔 id다. 클라이언트는 이 값을 보내지
  않는다.
- **하나의 주소 공간(SD4).** 세션, 상주 에이전트, `operator`는 기본으로 서로의 peer다. hop, 스레드, 크기, 빈도 제한은 모든
  쌍에 걸리고, 운영자는 주소마다 `peers`를 좁힐 수 있다. 어느 peer의 메일도 아무 권한을 주지 않는다.

## 세션 쪽

워크스테이션의 모든 것은 패키지 없는 표준 라이브러리 Python 3이며, `scripts/install-session-link.sh`(네이티브 Windows는
PowerShell 래퍼 `install-session-link.ps1`)가 `~/.guardian/session/`에 설치한다.

| 플랫폼 | 데몬 | 소켓 | 깨우기 |
|---|---|---|---|
| macOS | LaunchAgent | 세션별 Unix 소켓 | tmux |
| WSL | systemd 사용자 유닛(WSL에서 systemd가 켜져 있어야 한다. 꺼져 있으면 설치기가 알리고 멈춘다) | 세션별 Unix 소켓 | tmux |
| 네이티브 Windows(SD7) | 로그온 시 예약 작업(S0w에서 예약 작업으로 부족하다고 나올 때만 Windows 서비스) | 세션별 named pipe(`multiprocessing.connection`, `AF_PIPE`) | 없음, 대기열만 |

### 세션 MCP 도구

| 도구 | 답하는 곳 | 하는 일 |
|---|---|---|
| `session_link` | 허브 | 이 세션의 주소(이름)를 잡거나 지금 주소를 알려 준다 |
| `session_inbox` | 데몬(스풀) | 읽지 않은 메일, 오래된 것부터. 이 세션에서 가져갔다고 기록한다 |
| `session_send` | 허브 | 한 주소로 메시지 하나 |
| `session_list` | 허브 | 이 세션이 쓸 수 있는 주소, 상태와 마지막으로 보인 시각 포함 |
| `session_status` | 데몬 | 연결 상태, 주소, 읽지 않은 수, 원격 승인(이 세션)과 요약 미러가 켜져 있는지 |

MCP는 사용자당 한 번 추가하므로(`claude mcp add --scope user`) 프로젝트 파일 없이 모든 대화형 세션이 갖는다. 네트워크
소켓은 열지 않는다. 유일한 상대는 이 세션을 위한 데몬의 소켓이다.

### 훅

| 훅 | 용도 |
|---|---|
| `SessionStart` | CLI의 세션 id를 데몬에 등록하고, 세션별 소켓을 받고, 세션이 tmux 안에서 도는지(`$TMUX`, pane id) 기록한다. 상태 `linked`(또는 `session_link` 전까지는 미연결) |
| `UserPromptSubmit` | 기다리는 메일이 있으면 맥락 한 줄을 더한다: "새 메시지 N개, session_inbox로 읽으세요"(내용은 절대 넣지 않는다) |
| `Stop` | 턴 도중 메일이 왔고 세션이 `away`가 아니면 고정 사유 "new mail: call session_inbox"로 멈춤을 한 번 막는다. 그 사이에 `session_inbox` 호출이 없으면 두 번 막지 않는다(상주 `Stop` 이어가기와 같은 조건). 미러가 켜져 있으면 턴 요약을 시작한다(SD6a) |
| `PermissionRequest` | 이 세션에 원격 승인이 켜져 있으면(SD5, SD11): 요청을 허브에 올리고 운영자의 답을 120초까지 기다린다. 시간이 지나거나 오류가 나면 평소의 로컬 확인 창으로 넘어간다 |
| `SessionEnd` | 주소를 놓는다. 그 뒤 그 주소로 온 메일은 만료되거나 같은 이름이 다시 연결될 때까지 허브에 `queued`로 남는다 |

권한 우회(bypass-permissions) 모드라고 훅이 꺼지지는 않는다(S0 4부, claude 2.1.329). bypass 세션은 보통의 도구 호출에서
확인 창을 건너뛰지만, 맞는 `permissions.ask` 규칙(프로젝트 설정이나 플래그 설정)이 있으면 확인 창을 띄우고, 사람은 세션
도중에 bypass에서 나갈 수 있다(shift+tab: auto, manual, accept edits, plan). 두 경우 모두 `PermissionRequest`가 발생하고
훅의 답이 그대로 따라진다. 입력의 `permission_mode`(`bypassPermissions`, `default`, ...)로 두 경우를 가를 수 있고, 운영자가
참고하도록 요청과 함께 기록할 뿐이다: 모드와 상관없이 SD11의 `GUARDIAN_REMOTE_APPROVAL=1`이 정한다. bypass 세션을 쓰는
운영자는 고른 명령(예: `git push`)을 ask 규칙 뒤에 두고 원격으로 답할 수 있다. auto 모드(분류기가 확인 창에 답함)는 모드
순환에서 보였지만 시험하지 않았다. 데몬과 설치 프로그램은 `skipDangerousModePermissionPrompt`를 쓰지 않고 CLI를 bypass
플래그로 띄우지 않는다: 모드는 사람이 고른다.

### 깨우기 (SD3, SD3a)

키보드 앞에 아무도 없는 쉬고 있는 세션에서는 발생하는 훅이 없다. 운영자는 그 경우에 tmux 깨우기를 허용했고, `operator`가
보낸 메일로 한정했다(SD3a). 데몬은 다음이 **모두** 맞을 때만 세션의 tmux pane에 깨움 문장을 입력한다.

1. 깨우기를 일으킨 메일이 `operator`(대시보드에서 `AGENT_TOKEN`으로 보냄)에게서 왔다. 세션과 상주 에이전트의 메일은
   대기열에 들어가 훅으로 드러나며, 깨우기로는 절대 드러나지 않는다.
2. 세션이 `idle`(연결됨, 10분 동안 턴 없음)이고 `away`가 아니다.
3. 세션의 pane이 `SessionStart`에서 기록되었고 여전히 같은 CLI 프로세스를 돌린다.
4. tmux 클라이언트에 최근 60초 동안 입력이 없었다(`client_activity`). 즉 누가 치고 있지 않다.
5. pane의 입력 줄이 비어 있다(`tmux capture-pane`으로, S0에서 찾은 CLI의 빈 입력 패턴과 맞춰 본다).
6. 최근 10분 안에 이 세션에 깨움 문장을 입력한 뒤 `session_inbox` 호출이 없었던 적이 없다.

깨움 문장은 고정 문장 "new mail from operator: call session_inbox"이고, `tmux send-keys -l`과 Enter로 보낸다. 메일의 어느
부분도 담지 않는다. tmux 밖의 세션과 네이티브 Windows의 모든 세션은 대기열만이다. 메일은 다음 입력이나 턴 끝을 기다리고,
대시보드는 읽지 않음으로 보여 준다.

### 상태

| 상태 | 뜻 |
|---|---|
| `linked` | 스트림 연결됨, 세션 묶임, 도는 턴 없음 |
| `busy` | 턴이 도는 중(`UserPromptSubmit`과 `Stop` 사이) |
| `idle` | 연결됨, 10분 동안 턴 없음 |
| `away` | 사람이 자리 비움으로 표시함(`session_link(away=true)`): 메일은 받아 대기열에 두고, `Stop` 이어가기와 깨우기는 끈다 |
| `offline` | 스트림 끊김(90초 동안 연결 프로토콜 heartbeat 실패, 워크스테이션이 잠들었거나 tailnet에서 빠짐), 또는 CLI가 `SessionEnd` 없이 끝남 |

대시보드는 세션마다 깨울 수 있는지(tmux 안, 네이티브 Windows 아님)도 보여 준다.

### 요약 (SD6, SD6a)

운영자가 대시보드에서 세션을 따라갈 수 있도록 허브는 턴의 짧은 요약을 둔다. 대화 기록 자체는 워크스테이션을 떠나지 않는다.

- **모델이 쓴다(SD6a).** `Stop`에서 훅이 요약을 백그라운드 자식으로 시작하므로 턴은 바로 끝난다. 요약은 **세션이 쓰는 것과
  같은 제공자**가 쓴다(claude라면 작은 모델과 도구 없이 `claude --print`). 그래서 턴을 보는 새 당사자가 생기지 않는다.
  입력: 턴의 사용자 입력과 마지막 어시스턴트 글. 도구 호출이나 도구 출력은 절대 넣지 않는다.
- **한도.** 세션당 10분에 요약 하나까지(그 사이의 턴은 다음 요약에 접어 넣는다), 출력은 2 KiB까지, 실패하거나 60초를 넘긴
  요약 호출은 버리고 다시 시도하지 않는다.
- **나가기 전에 거른다.** 데몬은 보내기 전에 비밀처럼 보이는 줄(`is_secret` 패턴과 키/토큰 모양)을 지운다. 허브가 다시
  확인하고 여전히 걸리는 요약은 거부한다.
- **7일 보관** 후 허브에서 지운다. 야간 백업에 들지 않는다.
- **할당량.** 요약마다 세션 제공자의 할당량을 쓴다. 미러는 워크스테이션별로 켜지고 Settings에서 끌 수 있다. 정확한 모델,
  간격, 한도는 S0에서 확인하고 S5에서 정한다.

## 권한과 한도

- **메일은 아무 권한도 주지 않는다.** 세션이 보냈든 에이전트가 보냈든 같다. 동작, 계획, 권한을 승인하지 못하고 동의를
  넘겨주지도 못한다. 세션 MCP의 도구 설명과 온보딩 문장이 이를 밝힌다. 기본 peer(SD4)도 이를 바꾸지 않는다.
- **원격 승인은 운영자의 것.** 대시보드에서 `AGENT_TOKEN`으로 준다. 메일, 다른 세션, 에이전트가 주는 일은 없고, 요청받은
  `PermissionRequest` 하나(요청 id, 세션, 도구, 인자의 해시, 기한)에만 답한다. 허브는 누가 답했는지 기록한다. 시간 초과나
  오류는 절대 허용이 되지 않는다. 허브가 묻기라도 할 수 있는 것은 사람이 `GUARDIAN_REMOTE_APPROVAL=1`로 시작한 세션뿐이다
  (SD11).
- **깨우기는 운영자의 것**(SD3a): `operator`의 메일만 세션을 깨울 수 있다.
- **한도**는 상주 표(hop 3, 스레드당 메시지 40개, 32 KiB, 수명 24시간)를 다시 쓰고, 여기에 더한다: 세션당 시간당 보내는
  메시지 30개, 세션당 대기 중 원격 승인 10개, 세션당 10분에 깨우기 1번, 세션당 10분에 요약 1개.
- **끄는 스위치:** 허브의 `GUARDIAN_SESSIONS=off`면 연결 진입점이 모든 스트림을 거부한다. Settings에서 워크스테이션을
  지우면 그 줄을 뺀 `authorized_keys2`를 다시 쓰고 스트림을 바로 끝낸다. 워크스테이션에서는 `install-session-link.sh --disable`이
  데몬을 멈추고 로컬 키를 지운다.

## 대시보드

Nodes 아래의 Sessions 페이지:

- 워크스테이션과 그 세션 목록: 주소, 상태, 읽지 않음, 마지막으로 보인 시각, 깨울 수 있음, 원격 승인 켜짐/꺼짐.
- 세션마다: 받은편지함과 스레드 보기, 메시지를 보내는 칸(운영자의 메일은 세션을 깨울 수 있다), 대기 중인 권한 요청과
  허용 / 거부, 남은 시간, 최근 7일의 요약.
- Settings -> Workstations: 워크스테이션 등록(id, tailnet 노드, 설치기가 출력한 공개 키. 페이지는 설치기가 고정할 허브
  호스트 키의 지문, 허브의 tailnet IPv4, 운영자가 정했다면 연결 이름을 보여 준다, SD12), 제거, 워크스테이션별 원격 승인과 요약 미러 켜기/끄기(등록 시 둘 다 켜짐. 워크스테이션의 원격 승인은 그곳 세션이 옵트인할 수 있게
  할 뿐이다, SD11). 이미 쓰이는
  키나 id, 주소가 없는 tailnet 노드의 등록은 거부한다. 줄의 `from=`은 등록 때 노드의 tailnet 주소를 쓴다(SD9). 노드가 새
  주소로 다시 등록되면 페이지가 불일치를 보여 주고, 운영자가 누르면 줄을 다시 쓴다(그때까지 새 주소의 연결은 거절된다).
- 새 문자열은 모두 `ko`와 `en` 카탈로그에 넣는다.

## 결정

운영자가 2026-11-20에 내렸다. "다름"은 2026-11-17 권장안과 다른 답을 표시한다.

| # | 질문 | 결정 | 다름 |
|---|---|---|---|
| SD1 | 스트림을 누가 여나 | 워크스테이션이 tailnet 위로 허브의 연결 진입점에 연다. 각 워크스테이션은 태그 단 노드로 tailnet에 가입하고 SSH 서버를 돌리지 않는다. 허브는 운영자가 허락할 때만, 기한이 있는 별도 키로 워크스테이션에 닿는다(규칙 3) | 아니오(2026-11-20 고침. 같은 날의 첫 답: 허브가 접속) |
| SD2 | 워크스테이션 자격 증명 | 워크스테이션마다 그 장비에서 만든 키 하나. 허브에서 `command=` + `restrict`로 연결 진입점과 워크스테이션 id에 묶는다. 허브의 호스트 키는 워크스테이션에 고정. 거기에 `AGENT_TOKEN`은 없다 | 아니오(2026-11-20 고침. 첫 답: 허브 키 하나를 모든 워크스테이션에) |
| SD3 | 쉬고 있는 세션에 원격 입력 | "깨우기"의 조건 아래 tmux 깨우기 허용. tmux가 없거나 네이티브 Windows면 대기열만 | 예(이전: 대기열만) |
| SD3a | 어떤 메일이 세션을 깨울 수 있나 | `operator`의 메일만 | —(SD3, SD4 뒤 2026-11-20에 물음) |
| SD4 | 세션과 상주 에이전트를 한 주소 공간에 | 예, 그리고 기본으로 서로의 peer. 한도는 그대로 | 예(이전: 종류 사이에는 기본 peer 없음) |
| SD5 | 권한 확인 창 원격 승인 | 만든다. 워크스테이션을 등록하면 켜지고 Settings에서 끈다. 기한 120초, 시간 초과면 로컬 확인 창으로, 자동 허용 없음 | 기본값 예(이전: 옵트인). SD11이 좁힘 |
| SD6 | 허브로 대화 기록 미러 | 요약만, 도구 출력은 절대 없음, 7일 보관. S5가 범위에 든다 | 예(이전: 끔) |
| SD6a | 요약을 누가 쓰나 | 모델, 세션 자신의 제공자. 10분에 하나까지, 2 KiB, 비밀 필터 | —(권장: 마지막 어시스턴트 글, 모델 없음) |
| SD7 | Windows | WSL과 네이티브 Windows | 예(이전: WSL만) |
| SD8 | 벤더 Remote Control과의 관계 | 둘 다 유지. 이 설계는 API 키 CLI와 반드시 도착해야 하는 메시지를 위한 것이고, 휴대폰 UI는 벤더 기능을 쓴다 | 아니오 |
| SD9 | 연결 키를 주소에 묶기 | 예. 모든 연결 줄에 `from="<workstation tailnet address>"`를 단다. 주소는 등록 때 tailnet 노드에서 가져오고, 노드가 새 주소를 받으면 Settings에서 다시 쓴다. 허브의 sshd는 headscale ACL이 덮지 않는 공인 주소에서도 듣는다 | — (S0 3부 뒤 제안, 2026-11-20 결정) |
| SD10 | 연결 진입점을 누구로 돌리고 연결 줄을 어디에 두나 | 허브 사용자. 줄은 `~/.ssh/authorized_keys2`에 두고 Guardian이 그 파일을 통째로 맡는다(등록부에서 쓰고, 임시 파일과 rename, 모드 600). 운영자의 `authorized_keys`는 건드리지 않는다. 허브의 sshd가 이미 `authorized_keys2`를 읽으므로 sshd는 바꾸지 않는다. 등록할 때 이것이 그대로인지 확인하고, Guardian 머리말 없는 파일이 있으면 거부한다 | 아니오(권장안. 열린 점 3, 2026-11-20 결정) |
| SD12 | 연결 데몬이 접속할 허브 주소 | 먼저 허브의 tailnet IPv4. 설치 때 Settings에서 정한다(`--hub-address`). 접속이 실패하면 한 번 허브의 연결 이름(`--hub-name`): 운영자 자신의 도메인(Cloudflare, DNS 전용, 프록시 없음)에 같은 tailnet IPv4로 둔 A 레코드로, 운영자가 만들고 관리하며 Guardian은 손대지 않는다. 두 길 모두 `HostKeyAlias <hub id>`, 호스트 키가 다르면 접속을 끝낸다. 공인 주소는 쓰지 않는다(SD9의 `from=`이 거부) | 일부(권장안: tailnet IPv4만. 운영자가 DNS 이름을 더함, 2026-11-20 결정) |
| SD11 | 원격 승인을 누가 켜나 | 워크스테이션의 사람이 세션마다: CLI를 `GUARDIAN_REMOTE_APPROVAL=1`로 시작한다(훅이 자기 환경을 읽으므로 메일도 세션의 모델도 켤 수 없다). 세션 MCP는 끄기만 할 수 있다. SD5의 워크스테이션 스위치는 상한으로 남는다 | 아니오(권장안. 열린 점 2, 2026-11-20 결정. SD5를 좁힘) |

SD1과 SD2는 S0 2부 뒤 2026-11-20에 운영자가 고쳤다. 모든 워크스테이션에 접속하는 허브는 제어권을 쥐고, 워크스테이션마다
SSH 서버가 필요하다. 대신 워크스테이션이 제어권을 쥔다: 연결을 스스로 열어 허브에 요청을 내리고, 허브에는 필요할 때만 SSH를
허락한다. 방향은 세션이 연결되어 있는 동안 허브가 프로토콜 안에서 할 수 있는 일(SD5의 원격 승인, SD3의 깨우기)을 바꾸지
않는다. SD11이 앞의 것을 좁힌다: 탈취된 허브는 사람이 원격 승인을 켜고 시작한 세션의 권한 확인 창에만 닿는다.

## 단계

| 단계 | 내용 | 끝 조건 |
|---|---|---|
| S0 프로브 | tailnet에 가입한 macOS 워크스테이션 하나와 WSL 워크스테이션 하나에서: 워크스테이션별 키와 허브의 `command=`,`restrict` 한 줄로 워크스테이션에서 허브로 접속(셸, PTY, 포워딩 없음, `SSH_ORIGINAL_COMMAND` 거부, 인사말 확인, 허브 자신의 sshd와 로그인 셸 아래), 대화형 세션의 사용자 범위 MCP, `UserPromptSubmit` 추가 맥락, `Stop` 한 번만 막기 조건, `PermissionRequest` 훅의 대기와 넘어가기, 그리고 bypass 모드에서 언제 발생하는지(4부에서 함), `SessionStart` 세션 id와 tmux pane, tmux 깨우기 조건(idle, `client_activity`, `capture-pane`으로 빈 입력 줄, CLI의 빈 입력 패턴), 요약 호출(모델, 시간, 크기), 잠들기와 깨어나기를 거친 동작(2026-11-20 운영자가 뺌: S1과 S2가 멈추거나 죽은 스트림으로 대신 시험) | M0처럼 CLI 버전별 표를 `docs/analysis/`에 |
| S0w 프로브 | 네이티브 Windows 워크스테이션 하나에서: Python에서 돌리는 내장 OpenSSH 클라이언트(파이프 위의 `ssh.exe -T`), 그것이 요구하는 키 파일 권한, 고정한 허브 호스트 키, 표준 라이브러리 named pipe, Windows CLI 아래의 훅과 MCP(`py` 런처의 Python 또는 PowerShell), 로그온과 잠들기를 거친 예약 작업 | `docs/analysis/`에 표 |
| S1 허브 | `mail_store.py`의 주소 종류 `session`, `session_registry.py`, 워크스테이션 등록부(id, tailnet 노드, 공개 키), 연결 진입점 `guardian.py --session-link`(강제 명령, 인사말, 워크스테이션당 스트림 하나), 등록부에서 허브의 `authorized_keys2` 전체 쓰기(줄마다 `from=`, SD9, SD10)와 sshd가 그 파일을 여전히 읽는지 확인, `--sessions` CLI, mesh join의 워크스테이션 태그 | 가짜 스트림으로 단위 테스트. 등록되지 않은 키, 비어 있지 않은 `SSH_ORIGINAL_COMMAND`, stdin의 터미널, 다시 쓴 이름 거부. 한 워크스테이션의 새 스트림이 핸드셰이크 뒤에만 이전 스트림을 끝냄 |
| S2 워크스테이션 | `session_link.py` 데몬(허브의 tailnet IPv4, 그다음 연결 이름으로 접속(SD12), 다시 접속, 허브 호스트 키 고정), 세션 MCP, 훅, tmux 깨우기, LaunchAgent와 systemd 사용자 유닛 설치기(키 생성) | 두 워크스테이션의 실제 세션 둘이 허브를 거쳐 메일을 주고받음. 멈추거나 죽은 스트림(절전 대신)을 양쪽 heartbeat가 찾아내고 다시 접속하며, 그동안 쌓인 메일이 도착함. 운영자 메일은 쉬고 있는 tmux 세션을 깨우고 peer 메일은 깨우지 않음 |
| S2w Windows | named pipe 전송로, 예약 작업, PowerShell 설치기 | 네이티브 Windows 세션이 macOS 세션과 메일을 주고받음 |
| S3 대시보드 | Sessions 페이지, Workstations 설정 | 실제 브라우저에서 보내기, 읽기, 상태 표시 |
| S4 원격 승인 | `PermissionRequest` 경로, 허브 요청 표, 기한이 있는 허용/거부 | 실제 확인 창에 대시보드에서 답함. 시간 초과면 로컬로 넘어감 |
| S5 요약 | `Stop` 훅의 요약 호출, 필터, 7일 만료 허브 표, 대시보드 보기 | 실제 세션의 요약이 보임. 심어 둔 비밀 줄은 허브에 닿지 않음 |

S0w와 S2w는 S0, S2 뒤에 돌려 macOS와 WSL을 붙잡지 않는다.

2026-11-23부터 보류([verification-layer.ko.md](verification-layer.ko.md)의 DP5): S3, S4, S5는 S2 수용 시험 뒤에서
기다린다. 메일 저장소 부분(세션이 이름으로 참여, 메일은 권한을 주지 않음, 키는 허브가 보관)은 남고, 대화형 세션의 원격 제어
UX는 Guardian의 일이 아니다.

## 테스트

- 저장소: 세션 주소, 이름 충돌, 종류 사이 기본 peer, 좁힌 peer, 한도, 만료.
- 연결: 워크스테이션 id는 허브의 `authorized_keys2` 줄에서 오며 스트림의 필드가 아니다. 등록되지 않은 키, 비어 있지 않은
  `SSH_ORIGINAL_COMMAND`, stdin의 터미널은 거부한다. 데몬은 바뀐 허브 호스트 키를 거부한다. 한 워크스테이션의 새 스트림은
  인사말과 응답이 오간 뒤에만 이전 스트림을 끝내고, 그 전에 멈춘 접속은 이전 스트림을 건드리지 않는다. 끊긴 스트림은 대기
  메일을 한 번 다시 보낸다(작업 id). 워크스테이션을 지우면 그 줄만 빠진다. 쓰는 줄마다 노드의 tailnet 주소로
  `from=`이 붙고, 새 주소는 그 워크스테이션의 줄만 다시 쓴다. 파일은 제자리에서 고치지 않고 rename으로 바꾼다. 운영자의
  `authorized_keys`는 앞뒤로 바이트가 같다. Guardian 머리말 없는 `authorized_keys2`가 있거나 sshd가 그 파일을 더 이상 읽지
  않으면 등록을 거부한다. 데몬은 저장한 IPv4를 먼저 쓰고, 접속이 실패했을 때만 연결 이름을 쓰며, 호스트 키가 다를 때는 쓰지
  않는다. 두 길 모두 같은 `HostKeyAlias` 항목을 쓴다. 허브의 공인 주소를 가리키는 연결 이름은 `from=`이 거부한다(SD9, SD12).
- 훅: `Stop`은 연달아 두 번 막지 않는다. `UserPromptSubmit`은 메시지 내용을 넣지 않는다. `PermissionRequest` 시간 초과는
  "로컬에서 묻기"를 돌려준다. CLI 환경에 `GUARDIAN_REMOTE_APPROVAL=1`이 없으면 훅은 워크스테이션 스위치나 메일과 상관없이
  요청을 올리지 않는다.
- 깨우기: `operator` 메일만 깨운다. 입력되는 줄은 고정 문장이고 메일 내용을 담지 않는다. busy, away, 최근 클라이언트 입력,
  비어 있지 않은 입력 줄일 때, 그리고 `session_inbox` 없이 10분 안에 두 번은 깨우지 않는다. tmux 밖과 네이티브 Windows의
  세션은 절대 깨우지 않는다.
- 요약: 입력에 도구 호출이나 출력이 들지 않는다. 비밀처럼 보이는 줄은 데몬이 지우고 허브가 거부한다. 10분에 하나.
  7일 뒤 만료.
- 승인: 다른 요청 id, 바뀐 인자 해시, 지난 기한에 대한 답은 거부한다.
- fixture에 실제 호스트나 계정 이름 없음(`ws-mac-1`, `ws-wsl-1`, `ws-win-1`, `site-a-arm1`).

## 위험

- **peer 메일과 자율 세션.** 기본 peer(SD4)이므로 열린 도구를 가진 에이전트가 bypass 모드로 도는 세션에 메일을 쓸 수 있다.
  메일은 아무 권한도 주지 않지만, bypass 모드에서는 메일과 셸 사이에 모델 자신의 판단만 남는다. 완화: 메일은 사람의 입력이나
  턴 끝에서만 드러나고, 깨우기는 `operator` 메일로 한정되며(SD3a), 깨움 문장은 내용을 담지 않고, 운영자는 `peers`를 좁힐 수
  있으며, ask 규칙은 bypass 모드에서도 고른 명령을 확인 창 뒤에 두어 로컬이나 원격으로 답하게 한다(S0 4부; `deny` 규칙은
  시험하지 않음).
- **깨우기가 그래도 사람과 부딪칠 수 있다.** 확인과 키 입력 사이에 사람이 치기 시작하는 경우다. 60초 무입력 창과 빈 줄
  확인으로 드물게 만들고, 입력되는 줄은 그 자체로 해가 없다.
- **tmux 밖과 네이티브 Windows의 쉬고 있는 세션은 메일을 듣지 못한다.** 사람이나 턴이 깨울 때까지.
- **훅 동작은 CLI 버전에 따라 다르다.** 설치기는 S0를 통과한 버전을 기록하고, CLI 버전이 바뀌면 데몬이 경고한다.
- **연결 진입점은 허브 사용자로 돈다.** sshd는 `AGENT_TOKEN`, fleet 키, 운영 데이터를 가진 계정의 권한으로
  `--session-link`를 띄운다. `restrict`와 강제 명령이 클라이언트를 셸 밖에 두므로, 공격면은 진입점 자신의 입력 처리다.
  진입점은 줄 프로토콜만 읽고, 클라이언트가 보낸 것을 절대 실행하지 않으며, 세션 연결 코드만 불러온다. 전용 사용자도
  따져 봤지만 고르지 않았다(SD10): 새 계정, sshd 변경, 메일 저장소 권한 분리가 필요한데, 강제 명령과 `restrict`가 이미
  범위를 묶는다.
- **키 파일의 Guardian 버그**는 연결 줄만 망가뜨린다: Guardian은 `authorized_keys2`만 쓰고 운영자의 `authorized_keys`는
  절대 쓰지 않으므로, 운영자를 허브에서 잠가 버릴 수 없다(SD10).
- **워크스테이션 키를 도둑맞으면** 그 워크스테이션 세션들로서 메일을 읽고 보낼 수 있지만, 허브의 셸을 얻거나 fleet이나 다른
  워크스테이션에 닿거나 승인에 답할 수는 없다(`AGENT_TOKEN`). 허브에서 그 줄을 지우면 무효가 된다. 이 설계를 위해 SSH 서버를
  돌리는 워크스테이션은 없으므로, 운영자 장비의 SSH 서버라는 이전 위험은 사라졌다.
- **허브의 sshd는 공인 주소에서도 듣는다.** headscale ACL은 tailnet 길만 막는다. 그래서 모든 연결 줄에 워크스테이션의
  tailnet 주소로 `from=`을 달고(SD9), 훔친 연결 키는 그 주소에서만 통한다. 인증된 `-N` 연결은 세션 채널을 열지 않아 연결 진입점에 보이지 않는다.
  훔친 키로 sshd 한도까지 그런 빈 연결을 붙잡아 둘 수 있다(위험 낮음).
- **연결 이름은 tailnet 주소를 담은 공개 DNS다(SD12).** 허브의 tailnet IPv4와 이름표가 드러나므로, 레코드 이름은 허브의
  별칭이 아닌 중립적인 것으로 한다. 그 주소는 tailnet 밖에서 닿지 않는다. 많은 가정용 공유기와 리졸버가 사설이나 CGNAT
  대역을 가리키는 공개 응답을 버리므로(DNS 리바인딩 보호, 예: dnsmasq `stop-dns-rebind`, Pi-hole) 어떤 네트워크에서는 이름이
  소리 없이 실패할 수 있다. 저장한 IPv4가 먼저인 까닭이다. 다른 호스트를 가리키는 이름은 고정한 호스트 키에서, 허브의 공인
  주소를 가리키는 이름은 `from=`에서 실패한다(SD9).
- **원격 승인은 사람의 장비를 원격으로 다루는 것이다.** 사람이 `GUARDIAN_REMOTE_APPROVAL=1`로 시작한 세션에만 닿고(SD11),
  워크스테이션별로 끌 수 있으며, 자동 허용은 없고, 모든 답은 감사 기록에 남는다.
- **요약은 워크스테이션을 떠난다.** 턴에 대한 모델의 글이라 코드나 이름을 인용할 수 있다. 필터는 비밀 모양을 잡을 뿐 민감한
  줄을 모두 잡지는 못한다. 7일 보관, 워크스테이션별로 끌 수 있다.
- **할당량.** 세션과 에이전트 사이의 메일, 그리고 모든 요약이 모델 할당량을 쓴다. 위 한도가 그 범위를 묶는다.

## 참고

- 벤더 기능 노트(Remote Control과 세션 간 메시징이 하는 일, 링크 포함, 2026-11-17 확인):
  [../analysis/2026-11-17-remote-control-reference/notes.ko.md](../analysis/2026-11-17-remote-control-reference/notes.ko.md)
- [resident-agents.ko.md](resident-agents.ko.md): 메일 저장소, 봉투, 전달, 한도, `Stop` 이어가기(다시 씀)
- [resident-open-tools.ko.md](resident-open-tools.ko.md): 도구를 가진 에이전트의 샌드박스와 수동 승인 규칙
- [remote-agents.ko.md](remote-agents.ko.md): 워커 프로토콜과 "모든 연결은 허브가 연다" 규칙. fleet 노드와 상주
  에이전트에는 적용되지만 워크스테이션에는 적용되지 않는다(SD1 고침)
- [mesh-join.ko.md](mesh-join.ko.md): 워크스테이션에 쓰는 tailnet 가입

## 열린 점

1. **세션의 작업 보드.** S1-S3에서는 세션에 내주지 않는다. 세션이 작업을 맡을 수 있는지는 S2 뒤에 정한다.

열린 점 2(세션별 원격 승인)와 3(연결 진입점을 누구로 돌리나)은 2026-11-20에 SD11과 SD10으로 정했다.

## 구현 메모

2026-11-20: SD1-SD8, SD3a, SD6a 결정, S0 3부 뒤 SD9-SD11 결정, 같은 날 SD12(허브 주소: tailnet IPv4, 그다음
운영자가 권장안에 더한 DNS 전용 이름) 결정. SD1/SD2는 처음에 원래 방향을 뒤집었다(상주 노드처럼 릴레이가
워크스테이션에 접속). S0 2부 뒤 운영자가 같은 날 이를 고쳤다: 워크스테이션이 첫 초안의 허브 쪽 강제 명령 진입점
(`guardian.py --session-link`)으로 워크스테이션별 키를 써서 연결을 연다. 허브는 세션 키를 두지 않고 워크스테이션은 SSH
서버를 돌리지 않는다.

2026-11-20, S0 1부(`ws-mac-1` 로컬 프로브, claude 2.1.329, tmux 3.8;
`../analysis/2026-11-20-s0-session-probe/results.ko.md`). 훅, 세션 MCP 묶기, `Stop` 막기, tmux 깨우기 조건 두 가지, 요약
호출이 동작한다. 다음 단계가 반영해야 할 정정:

- `PermissionRequest`는 로컬 대화상자보다 먼저 기다리지 않고 그것과 경주한다. 대화상자는 바로 뜨고, 훅이 답하면 그것으로
  정해지며, 로컬에서 답하면 훅이 SIGTERM을 받는다. SD5의 뜻(시간 초과나 오류로 허용하지 않음)은 그대로이고, "로컬
  대화상자로 넘긴다"는 "로컬 대화상자가 남아 있다"는 뜻이다. 설치하는 훅은 `timeout`이 약 130초여야 하고 SIGTERM에서
  허브 요청을 거둬야 한다. 기본 모드는 auto였고, auto에서는 무해한 호출에 `PermissionRequest`가 오지 않는다.
- Esc로 끊으면 `Stop`이 오지 않는다. `busy`는 다음 `UserPromptSubmit`이나 `SessionEnd`, 또는 턴 나이 상한에서 끝낸다.
- 빈 입력 줄: 기록 배경이 아닌 마지막 `❯` 줄이 `❯` + U+00A0 뒤에 흐린 부분만 있을 때. CLI 버전에 묶이며 fail closed.
- `client_activity`: `list-clients`에서 최댓값으로 읽는다. `send-keys`는 값을 바꾸지 않는다. 클라이언트가 없을 때 조건 4
  통과는 제안이고 결정이 아니다.
- `--print` 자식도 훅을 부른다(`CLAUDE_CODE_ENTRYPOINT=sdk-cli`). 훅과 세션 MCP는 entrypoint가 `sdk-cli`이거나
  `GUARDIAN_SESSION_CHILD=1`일 때를 빼고 동작하고, 요약 자식은 `~/.guardian/session/` 아래 빈 디렉터리에서 돈다.
- 세션 MCP는 `initialize` 전에 오는 `server/discover`에 "method not found"로 답해야 한다. manual 모드에서는 읽기 전용
  도구에 허용 규칙이 필요하고(설치기가 쓰고 운영자가 확인), `PermissionRequest` 훅은 그 도구를 건너뛴다.

2026-11-20, S0 2부(버리는 키와 `ws-mac-1`의 `command=`,`restrict` 한 줄로 허브 접속, 끝난 뒤 둘 다 지움; 같은 결과
파일). 강제 명령은 언제나 돌고, 터미널, 에이전트, X11, 포워딩은 하나도 지나가지 않으며, 고정한 호스트 키가 지켜진다. 정정은
이제 허브의 연결 진입점에 적용된다(프로브는 Mac을 서버로 둔 반대 방향이었다. 이것들은 OpenSSH의 동작이고,
3부에서 허브 자신의 sshd와 로그인 셸에서도 확인했다):

- 클라이언트 명령도 subsystem(sftp)도 `SSH_ORIGINAL_COMMAND`에 담겨 강제 명령에 떨어진다. 연결 진입점은 이 값이 있고 비어
  있지 않으면 출력 없이 끝난다.
- sshd는 강제 명령을 로그인 셸로 돌리고, 그 시작 파일이 `PATH`를 정하며(Homebrew의 `bin`과 함께 tmux가 빠져 있었다) 무언가를
  출력할 수도 있다. `command=` 줄과 진입점에서는 절대 경로를 쓰고, `LANG`과 `PATH`는 진입점이 직접 정하며, 데몬은 다른
  무엇보다 먼저 고정된 버전 붙은 인사말을 확인한다.
- `-tt`가 거절돼도 강제 명령은 시작된다. 데몬은 `-T`로 접속하고, 진입점은 stdin이 터미널이면 거절한다.
- 고친 SD1에서는 데몬이 사람의 tmux 서버에 로컬로 닿으므로, 강제 명령에서 거기에 닿는지라는 S2의 질문은 없어졌다.

2026-11-20, S0 3부(운영자의 허락을 받아 허브 `authorized_keys`의 임시 `command=`,`restrict` 한 줄로 워크스테이션에서
접속, 끝난 뒤 줄과 키를 지웠고 파일은 백업과 바이트 단위로 같음; 같은 결과 파일). 허브의 로그인 셸은 인사말 앞에 아무것도
찍지 않고, `PATH`는 사용자 디렉터리가 없는 시스템 기본값이며, 2부의 거절은 허브에서도 모두 맞다. 바뀐 것:

- 거절된 `-tt`는 허브에서도 강제 명령을 띄우고, 그 프로세스는 첫 쓰기까지만 산다. "새 스트림은 이전 스트림을 끝낸다"
  규칙은 이제 핸드셰이크가 끝나기를 기다린다(구조, 규칙 2).
- 허브의 sshd는 keepalive를 보내지 않는다(`ClientAliveInterval 0`). "상태"의 90초 heartbeat는 양방향 연결 프로토콜
  heartbeat이고, 데몬은 `ServerAliveInterval`도 설정한다. 설정값에서 읽었고 절전은 돌리지 않았다(뺐다, 아래 참조).
- sshd는 공인 주소에서도 듣는다. 위험을 더했고 `from=`을 제안해 프로브에서 확인했다. 운영자가 같은 날 SD9로 정했다.
- `AuthorizedKeysFile`에 `authorized_keys2`가 있다. 운영자가 같은 날 SD10으로 골랐다(Guardian이 그 파일을 맡고 운영자의
  `authorized_keys`는 건드리지 않음). 같은 날 운영자가 SD11도 정했다: 원격 승인은 사람이 `GUARDIAN_REMOTE_APPROVAL=1`로
  시작한 세션에서만. 그래서 앞서 적은 `session_link(remote_approval=true)` 안은 버렸다(모델이 부를 수 있는 도구는 메일로
  부르도록 꾀일 수 있다).

- S0 4부(bypass 모드, 2026-11-20): bypass 모드에서는 `PermissionRequest`가 발생하지 않는다는 서술이 틀렸다(정정 11).
  "Hooks"는 이제 언제 발생하는지(ask 규칙, bypass에서 나가기), 거기서도 훅의 `allow`와 `deny`가 따라진다는 것, 그리고
  `permission_mode`는 관문이 아니라 참고 정보라는 것을 적는다. 플래그 설정 파일로 bypass 동의를 미리 할 수 있으므로
  (`skipDangerousModePermissionPrompt`), Guardian은 그 키를 쓰지 않는다.

S0는 끝나지 않았다: WSL과 S0w가 남아 있다. 절전과 복귀는 운영자가 2026-11-20에 뺐다: 워크스테이션에만 걸린 문제이고,
잠든 워크스테이션에는 어차피 메일을 읽을 사람이 없다. 중요한 것은 깨어난 뒤 링크가 저절로 돌아오는 것이다. 절전은 양쪽에
FIN 없이 조용해지는 스트림으로 보이므로, S1은 허브 쪽(입구가 heartbeat 한도를 넘게 조용한 스트림을 끝내고 메일은 `queued`로
남음)을, S2는 데몬 쪽(멈추거나 죽은 `ssh` 자식을 heartbeat로 찾아 다시 접속)을 흉내 낸 스트림으로 시험한다. 실제 절전은
쓰면서 지켜본다. 이제 어느 워크스테이션도 SSH 서버가 필요
없으므로 운영자는 Mac의 원격 로그인을 다시 꺼도 된다.

2026-11-21, S1(허브) 구현. 같은 날 `8e2179d` 배포(S1 커밋 `3a23abe`의 후손)로 허브에 올라가 있다. sshd 점검은 아래에 있다:

- 저장소(`mail_store.py` 위의 `mail_sessions.py`, 스키마 5): 표 `links`(워크스테이션당 한 줄: 임대, 상태, 이유)와
  `sessions`. 세션 쓰기는 모두 같은 `_write()` 트랜잭션 안에서 링크 임대를 확인하므로, 링크를 잃은 스트림의 쓰기는
  `superseded`로 거부되고 아무것도 바꾸지 않는다. `GUARDIAN_SESSIONS=off`면 모든 호출을 거부한다(`sessions_off`). 세션
  메일의 hop은 답하는 메일의 최고 hop + 1(상한 `HOP_MAX` 3), 세션당 시간당 30통, 10분 동안 상태가 없으면 `idle`로 보고,
  링크가 끊긴 세션은 10분 뒤 놓아준다(놓아줄 것이 없으면 쓰기 잠금 없이 돌아온다). 세션에는 `peers_changed` 알림을 보내지
  않는다. 상주 에이전트는 명단 알림이 아니라 세션이 보낸 메일로 세션을 안다(세션 MCP에 명단이 필요하면 S2에서 다시 본다).
- 등록부(`session_registry.py`): `<data_dir>/session-workstations.json`(id, `100.64.0.0/10` 안의 tailnet IPv4, 주석을 뺀
  `ssh-ed25519` 키, 선택 노드 이름). id는 `operator`, `hub`, 인벤토리 노드일 수 없다(인벤토리를 못 읽으면 거부).
  `authorized_keys2`는 등록부에서 Guardian 머리글과 함께 통째로 쓴다. 머리글이 없는 파일은 거부하고 `authorized_keys`는
  절대 쓰지 않는다. `command=`는 풀지 않은 `~/.guardian/current` 경로를 두어 새 릴리스에 다시 쓸 필요가 없다.
  `GUARDIAN_SESSION_KEYS_FILE`로 파일 위치를 옮긴다(테스트). sshd 확인은 `sshd -T`, 다음 `sudo -n sshd -T`를 시도하고
  실행하지 못했으면 그렇다고 말한다.
- CLI: `--session-add WORKSTATION --address IPV4 --key KEY [--node NAME]`, `--session-address`, `--session-remove`,
  `--session-list`, 강제 명령용 `--session-link --workstation ID`. 단계 표에는 `--sessions`라고 했지만 그 이름은 토론
  세션 옵션이 쓰고 있어 목록은 `--session-list`다.
- 링크 진입점(`session_link.py`, `--session-link --workstation ID`): `SSH_ORIGINAL_COMMAND`가 있으면 출력 없이 종료 코드
  2로 거부하고, 터미널 stdin, `GUARDIAN_SESSIONS=off`, 모르는 워크스테이션은 stderr 한 줄과 함께 거부한다. 강제 명령은
  systemd `EnvironmentFile`을 받지 못하므로 `~/.guardian/agent.env`를 직접 읽되 `GUARDIAN_*` 키만 옮기고(이미 있는 키는
  덮어쓰지 않으며 `AGENT_TOKEN`은 들어오지 않는다) 그 뒤 `LANG=C.UTF-8`, `PATH=/usr/bin:/bin`을 고정한다. 핸드셰이크
  20초(허브 greeting, 데몬 `hello`; `handshake_timeout`이나 `protocol_mismatch`는 claim 전에 끝나므로 실패한 접속이 살아
  있는 링크를 끊지 않는다), 그다음 `link_claim`과 임대가 든 `welcome`. 폴링 2초(등록부 삭제, 차단 스위치, `link_current`,
  쌓인 메일을 `deliver`로 내보내기, 끊긴 세션 놓아주기), ping 30초마다, 90초 동안 프레임이 없으면 liveness로 끝(`liveness`,
  메일은 `queued`로 남는다), 허브 요청은 데몬의 답을 20초 기다린다(답 없는 `deliver`는 `queued`로 남아 다음 폴링에 다시 나간다). 쓰기는 256프레임 큐를 가진 writer
  스레드를 거치고, 큐가 차면 스트림을 끝내며(`writer_backlog`) `bye`에는 5초 유예를 준다. 임대를 잃은 스트림은
  `superseded`로 답하고 `link_end` 없이 끝나므로 새 링크는 그대로 산다. 작업 명세와 다른 점: 진입 시 차단 스위치는 합친
  환경에서 읽는다(운영에서는 `mail_store.sessions_off()`가 읽는 값과 같다). 프레임 처리기의 예외는 `internal_error`, 저장소
  예외는 `store_error`로 답하고 둘 다 스트림을 유지한다. 해석할 수 없는 줄이나 객체가 아닌 프레임은 스트림을 끝낸다
  (`bad_json`, `bad_frame`). 메일이 링크가 끊긴 동안 쌓였으면 바로 나가도록 `welcome` 직후 폴링을 한 번 한다.
- mesh join: `tag:workstation`은 운영자가 `GUARDIAN_HEADSCALE_TAGS`에 넣었을 때만, 그리고 혼자서만 쓸 수 있다(다른 태그와
  함께면 `tags` 필드 오류로 거부). 이때 응답에 `workstation: true`와 합류한 IPv4로 쓴 `--session-add` 명령이 든 `next`
  줄이 붙는다.
- 허브 sshd 점검(2026-11-21, 운영자 승인, 배포된 릴리스): 예시 워크스테이션 하나(일회용 키, Mac의 tailnet IPv4)를
  `--session-add`로 등록하자 `authorized_keys2`에 한 줄이 써졌다(mode 600). `--session-list`는 그 줄이 정상이고
  `sshd -T`가 `authorized_keys2`를 읽는다고 보고했다. Mac에서 `ssh -F /dev/null -o IdentitiesOnly=yes`로 에이전트 없이,
  고정된 호스트 키로 접속한 결과:
  - 등록된 키로 허브의 tailnet IPv4에 `-T`: 예시 id가 든 greeting 프레임. stdin이 `/dev/null`이라 `hello` 전에 EOF로
    스트림이 끝났다(`bye stream_end`, lease 없음). 운영 저장소에는 아무것도 써지지 않았다.
  - 등록되지 않은 키: `Permission denied (publickey)`.
  - 등록된 키에 명령을 붙임(`sftp`, `id`): 종료 코드 2, 출력 없음.
  - 등록된 키에 `-tt`: `PTY allocation request failed`(`restrict`), 클라이언트는 출력 없이 종료 코드 255로 끝났다.
  - 등록된 키로 허브의 공인 주소에 접속: `Permission denied`. sshd 로그에 "correct key but not from a permitted host
    (... required=<tailnet IPv4>)"가 남아 `from=`(SD9)이 지켜짐을 확인했다.
  - 등록된 키에 `-W 127.0.0.1:9870`: `administratively prohibited`.

  이어서 `--session-remove`를 하자 헤더만 남은 `authorized_keys2`와 `[]`만 든 레지스트리가 남았다. 두 파일을 점검 전처럼
  지웠고 `--session-list`는 등록된 워크스테이션이 없다고 답한다. 운영자의 `authorized_keys`는 sha256이 그대로다.
  주의: zsh에서는 일반 변수에 담은 옵션이 `ssh`에 인자 하나로 넘어간다. 배열이나 bash를 쓴다.

2026-11-21, S2(워크스테이션) 구현, 설치 안 함: `cli/llm-agent/`의 워크스테이션 모듈 여섯 개, 표준 라이브러리만 쓴다
(`session_local.py`의 테스트가 import를 지킨다). `session_local.py`(로컬 계약: 프레임, 요청 검증, 응답 모양, 경로, 설정,
고정 문구. 허브 모듈에 맞춰 고정한 링크 상수 사본), `session_uplink.py`(다이얼러, 백오프, 스풀, 허브 스트림),
`session_daemon.py`(링크 데몬), `session_mcp.py`(도구 다섯 개), `session_hooks.py`(훅 네 개), `session_wake.py`(tmux
깨우기 가드). 설치 스크립트 `scripts/install-session-link.sh`와 `deploy/session/`의 LaunchAgent, systemd 사용자 유닛
템플릿. 허브 쪽: 링크의 `list` 프레임(묶인 세션의 상대 목록, 종류·상태·마지막으로 보인 시각. 운영자는 늘 들어 있고 행이
없는 주소는 `unknown`). 워크스테이션에는 아무것도 설치하지 않았고 허브는 `list` 프레임 말고 바뀐 것이 없다(배포 안 함).
설계와 다른 점과 고른 것:

- 데몬 이름은 `session_link.py`가 아니라 `session_daemon.py`다. 그 이름은 허브의 링크 진입점이다.
- 소켓: 훅 작업(`register`, `prompt_submit`, `stop`, `end`)만 받는 제어 소켓 하나와, 등록된 세션마다 MCP 작업(`link`,
  `inbox`, `send`, `list`, `status`)만 받는 소켓 하나. 데몬은 프레임이 들어온 소켓으로 세션을 알고, 신원 필드
  (`cli_session`, `from`, `address`, `sender`)가 든 프레임은 `bad_request`로 거부한다. 훅은 허브를 기다리지 않고(bind,
  state, unbind는 뒤에서 나간다) MCP 호출은 최대 20초 기다린다.
- 세션 이름으로 쓸 수 없는 이름: 훅이 데몬에 보내기 전에 버린다(`name` null, 이름 없이 시작). 그래도 그런 이름이 든
  프레임은 작업 명세처럼 경고와 함께 받지 않고 `bad_request`로 거부한다.
- `register`는 pane 옆에 `tmux_socket`(계약 초안의 이름은 `tmux`)을 실어 깨우기가 맞는 tmux 서버로 간다.
- 세션 표는 `run/sessions.json`에 둔다(mode 600, 원자적으로 교체). 시작할 때 CLI pid가 없어진 항목은 버리고 나머지는
  소켓을 다시 연다. 새 lease는 허브에서 그 워크스테이션의 행을 모두 offline으로 만들므로, 데몬은 다시 연결될 때마다
  이름 있는 세션을 모두 다시 bind하고 `busy`를 다시 보낸다.
- 훅은 `CLAUDE_PID`가 숫자로만 되어 있으면 그것을, 아니면 훅의 부모 프로세스를 CLI pid로 쓴다(S0 1부: CLI가 훅을 자식으로
  실행한다). 세션 MCP는 신원 필드를 전혀 보내지 않는다.
- 받은편지함 ack가 실패하면(링크 끊김) 메일은 허브에서 `delivered`로 남는다. 스풀이 메일 id로 중복을 걸러 다시 연결된
  뒤의 재전달은 해가 없다.
- 깨우기: 가드 4(60초 동안 클라이언트 입력 없음)는 붙은 tmux 클라이언트가 없으면 깨우지 않는 쪽으로 닫힌다. 가드 3은 CLI
  pid가 pane 프로세스이거나 8단계 안의 조상이면 받아들인다. 거절 이유는 로그에 남고, 거절된 깨우기는 메일이 안 읽힌 동안
  다음 틱에 다시 시도한다.
- 업링크의 링크 연결 콜백은 링크 스레드에서 돌므로 데몬은 자기 스레드에서 다시 bind한다. 전달은 링크 스레드를 막지 않는다
  (세션에 표시하고 틱 스레드에 신호만 준다).
- ssh는 `-F /dev/null`, `BatchMode`, `IdentitiesOnly`, `HostKeyAlias`를 쓴 전용 `known_hosts`, 전역 known hosts 없음,
  `ServerAliveInterval=30`, `ServerAliveCountMax=3`으로, `SSH_AUTH_SOCK` 없이 돈다.
- 설치 스크립트: tailnet IPv4를 `ssh-keyscan`한 호스트 키를 운영자가 Settings에서 복사한 지문과 비교해 고정한다. 정확히
  같을 때만 `known_hosts`를 쓰고, 다르면 아무것도 쓰지 않는다(종료 코드 4). 공개 키 줄, `claude mcp add` 명령,
  `~/.claude/settings.json`에 넣을 훅 블록, 읽기 전용 도구 세 개(`session_inbox`, `session_list`, `session_status`)의
  `permissions.allow` 제안은 출력만 하고 적용하지 않는다. `~/.ssh`와 `~/.claude/settings.json`은 건드리지 않는다.
  `--disable`은 워크스테이션 차단 스위치다. 서비스와 키를 지우고 설정, `known_hosts`, 스풀은 남긴다.
- 테스트: 종단 간 테스트(`test_session_e2e.py`)는 실제 데몬 두 개와 그 업링크, 허브의 `LinkSession`과 메일 저장소를 임시
  데이터 디렉터리에서 돌린다. ssh, CLI 세션, tmux, 시계는 흉내 낸다(무엇을 흉내 내는지는 docstring에 정확히 적었다).
  주의: `MailStore` 하나는 sqlite 연결 하나를 들고 그 연결은 연 스레드에서만 쓸 수 있으므로, 흉내 낸 스트림마다 저장소를
  따로 둔다.
- z.ai가 여러 조각으로 만들었다(계약과 허브 `list` 프레임, 훅, MCP, 깨우기, 업링크, 데몬, 그다음 설치 스크립트와
  종단 간 테스트). 마지막 조각은 z.ai 작업 트리에서 돌릴 수 없어 시험하지 않은 채 돌아왔으므로 종단 간 테스트를 손으로
  고쳤다: 다시 걸기 관문은 `threading.Event`(`clear`/`set`)이고, 받은편지함 ack 뒤 허브의 메일은 `delivered`가 아니라
  `acked`이며, 죽인 스트림은 쓰기 끝부터 닫는다(버퍼 있는 읽기 파일을 닫으면 막힌 `read()`가 쥔 잠금을 기다린다).
  링크가 끊긴 동안 쌓인 메일은 링크 연결 때의 폴이 아니라 다시 bind한 뒤 첫 폴에 나간다(새 lease가 막 세션을
  offline으로 만들었으므로). 설치 스크립트 테스트의 `launchctl bootout` 대상은 `gui/<uid>/<label>`로 바로잡았다.
- 2026-11-21 Mac에 처음 실제로 설치했다(워크스테이션을 허브에 등록, 허브의 `authorized_keys2`를 그 한 줄로 새로
  만듦, lease 1로 링크 `up`). 홈 디렉터리가 소유권이 꺼진 외장 APFS 볼륨에 있어(`diskutil info`가
  `Owners: Disabled`), launchd가 거기 있는 LaunchAgent plist를 모두 거부했다(`launchctl bootstrap` 오류 5, 같은
  plist를 부트 볼륨에 두면 bootstrap된다). `--no-service`로 다시 설치했고 데몬은 손으로 돌린다. 이제 설치 스크립트는
  1단계에서 홈 볼륨을 검사하고(launchd일 때만, `--dry-run`에서도) 아무것도 쓰기 전에 종료 코드 3으로 멈추며
  `--no-service` 또는 운영자가 직접 그 볼륨의 소유권을 켜는 방법(`sudo diskutil enableOwnership <mount>`, 설치
  스크립트는 절대 실행하지 않음)을 알려 준다. 볼륨을 읽지 못하면 경고만 하고, 9단계에서 bootstrap이 실패해도 같은
  힌트를 덧붙인다.
- 2026-11-22 허브 배포 및 Mac 적용: `list` 프레임과 설치기 소유권 검사가 든 `a6aa7e9`를 허브에 배포했다(상주 에이전트 셋
  모두 `ready`로 복구). Mac에는 `guardian-session` 사용자 MCP를 등록(`claude mcp get guardian-session`: 연결 확인)하고
  `~/.claude/settings.json`에 S2 훅 넷(`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, 5초 제한)과 읽기 전용 도구
  셋(`session_inbox`, `session_list`, `session_status`)의 `permissions.allow`를 병합했으며, `session_daemon.py`를 띄워 허브와
  링크 `up`(lease 2)을 확인했다.
- 2026-11-22 Antigravity(`agy`) 어댑터 추가 및 Mac 적용: Antigravity는 `CLAUDE_CODE_SESSION_ID` 없이 프로세스당 stdio
  MCP 자식을 하나 띄우고, `~/.gemini/config/hooks.json`에서 camelCase `conversationId`와 `PreInvocation`(`invocationNum == 0`,
  `injectSteps`)·`Stop`(`continuationActive`, `decision: "continue"`)을 쓰며 `SessionEnd` 이벤트가 없다. 이에 맞춰
  `session_mcp.py --client agy`(`agy-<ppid>` 키, 소켓 부재 시 제어 소켓에 자동 등록), `session_hooks.py`의
  `agy-session-start`/`agy-pre-invocation`/`agy-stop`(비활성·오류 시 항상 `{}` 출력, 소켓이 이미 있으면 재등록을 건너뛰어
  `/new`에서도 기존 바인딩 유지), `session_daemon.py._tick_once`의 종료된 PID 정리(소켓 닫기 및 허브 `unbind`)를 추가했다.
  Mac의 `~/.gemini/config/mcp_config.json`, `~/.gemini/antigravity-ide/mcp_config.json`, `~/.gemini/config/hooks.json`에
  적용하고 실제 `agy` 실행 시 실행 중인 데몬에 `agy-<pid>`가 등록되는 것을 확인했다.
- 하지 않은 것: WSL 호스트 설치와 S2 종료 조건의 실제 워크스테이션 두 대 인수 시험(tailnet 위 두 대, 실제로 죽인 스트림과
  조용해진 스트림).

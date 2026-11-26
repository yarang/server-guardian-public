# S0 1-4부: macOS 워크스테이션 한 대에서 대화형 세션 프로브, 허브 접속, 허브 쪽 링크 입구, bypass 모드 (2026-11-20)

[English](results.md) | 한국어

> macOS의 claude 2.1.329와 tmux 3.8에 대한 2026-11-20 시점 기록입니다. hook 입력, 환경 변수 이름, 입력창 모양은
> CLI 릴리스마다 바뀝니다.

설계: `../../design/operator-sessions.ko.md` (S0 단계; "Hooks", "Wake (SD3, SD3a)", "Summaries (SD6, SD6a)",
"Authority and limits" 절). **1부**는 워크스테이션 혼자서 돌릴 수 있는 것을, **2부**("2부: 허브 접속")는 강제 명령으로 묶은 전용
키로 허브가 SSH로 접속하는 것을, **3부**("3부: 허브 쪽 링크 입구")는 수정된 방향, 곧 워크스테이션이 허브의 강제 명령
줄로 접속하는 것을, **4부**("4부: bypass 모드")는 권한 우회 모드가 `PermissionRequest` 훅을 어떻게 다루는지를 다룹니다.
절전과 복귀, WSL과 네이티브 Windows는 남아 있습니다("하지 않은 것" 참조).

## 진행 방법

`ws-mac-1`(운영자의 Mac)에서 손으로, 버리는 디렉터리 안에서 돌렸습니다. 프로브 세션마다 별도 tmux 서버
(`tmux -L s0probe -f /dev/null`)에서, `env -i`로 CLI를 띄우는 실행기(`HOME`, `USER`, `PATH`, `TERM`, `LANG`, `TMUX`,
`TMUX_PANE`, 표식 하나만 유지)를 거쳐 다음 플래그로 실행했습니다.

```
claude --model haiku --setting-sources project --settings <probe>/settings.json \
       --mcp-config <probe>/mcp.json --strict-mcp-config [--permission-mode default]
```

사용자 설정은 일부러 뺐습니다. 운영자의 설정에는 열 가지 이벤트에 대한 hook이 있어 프로브 hook과 섞이기
때문입니다. `settings.json`은 `SessionStart`, `UserPromptSubmit`, `Stop`, `PermissionRequest`(matcher `*`),
`SessionEnd`에 stdlib Python hook 스크립트 하나를 등록했습니다. 스크립트는 stdin, 부모 pid, 환경(허용 목록은 값, 그 밖의
`CLAUDE*`/`ANTHROPIC*` 변수는 이름만)을 기록하고, 무엇을 답할지 적힌 제어 파일을 읽었습니다. `mcp.json`은 고정 표식을
돌려주는 도구 `session_inbox` 하나만 가진 stdio 스텁 서버를 등록했습니다. pane은 `tmux send-keys`로 조작하고
`tmux capture-pane`으로 읽었습니다. Guardian 코드는 돌지 않았고, 허브에 접속하지 않았으며, 운영자의 사용자 설정과
사용자 범위 MCP는 건드리지 않았습니다. 아래의 세션 id와 경로는 자리표시자입니다.

## 결과

| 확인 항목 (설계 항목) | 결과 |
|---|---|
| `SessionStart`: 세션 id, tmux pane | **통과.** 입력에 `session_id`, `source`(`startup`), `model`. hook 환경에 `TMUX`, `TMUX_PANE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CODE_ENTRYPOINT`가 있고, hook의 부모 pid는 CLI의 pid |
| 대화형 세션의 사용자 범위 MCP | `claude mcp add --scope user` 대신 `--mcp-config`로 **통과**. 발견 두 가지는 "MCP" 절 |
| `UserPromptSubmit` 추가 문맥 | **통과.** `additionalContext`가 모델에 닿음(심어 둔 단어를 그대로 말함). 입력에 `prompt`, `prompt_id`, `permission_mode`도 있음 |
| `Stop` 한 번만 막기 | **통과.** `{"decision":"block","reason":...}`로 턴이 이어지고, 두 번째 `Stop`은 `stop_hook_active: true`로 옴. **다만** Esc로 끊으면 `Stop`이 오지 않음 (정정 2) |
| `PermissionRequest` 대기와 넘김 | **동작하지만 방식이 다름** (정정 1): 로컬 대화상자와 hook이 동시에 돌고, 먼저 답한 쪽이 이김 |
| bypass 모드에서 `PermissionRequest`가 오지 않음 | **틀림** (4부, 정정 11): ask 규칙과 bypass에서 나가기가 훅을 부르고, 훅의 답이 그대로 따라짐 |
| tmux wake 조건: `client_activity` | **통과**, 단 "클라이언트 없음" 경우가 비어 있음 (정정 4) |
| tmux wake 조건: 빈 입력 줄 | **통과**, 정확한 패턴 확정 (정정 3) |
| 요약 호출 (모델, 시간, 크기) | **통과.** `claude --print --model haiku --tools "" --setting-sources project --strict-mcp-config --no-session-persistence < /dev/null`, 입력 1.2 KB(프롬프트+최종 답): 2.6초, 2.5초, 2.7초, 출력 448-475바이트, 비밀이나 경로 복사 없음. 설계의 60초, 2 KiB보다 한참 아래. 자식 세션 문제는 정정 5 |
| `SessionEnd` | **통과.** `/exit`에서 `reason: prompt_input_exit`로 옴 |
| 절전과 복귀 | 안 함 (운영자 필요) |
| `command=`,`restrict`로 허브 접속 | **통과**, 설계 정정 세 가지 (2부, 정정 6-8) |
| 워크스테이션이 허브의 `command=`,`restrict` 줄로 접속 | **통과**, 설계 정정 두 가지와 제안 하나 (3부, 정정 9-10, `from=`, SD9로 채택) |

## 설계 정정

### 1. 원격 승인은 로컬 대화상자와 경주한다. 먼저 기다리지 않는다

`PermissionRequest`를 등록하고 세션이 manual 모드이면, CLI는 로컬 권한 대화상자를 **즉시** 띄우고 hook을 동시에
돌립니다.

- 대화상자가 떠 있는 동안 hook이 `allow`(또는 `deny`)를 답하면 그것으로 결정되고, 대화상자는 "Allowed by
  PermissionRequest hook"으로 닫힙니다(3초, 8초에서 확인).
- 사람이 먼저 로컬에서 답하거나 Esc를 누르면, CLI는 아직 돌고 있는 hook에 **SIGTERM**을 보냅니다. hook은 이를 받아
  허브 요청을 거둘 수 있습니다.
- 설정한 `timeout`을 넘긴 hook은 제때 알아챌 신호 없이 종료되고, 대화상자는 그대로 남습니다.

따라서 설계의 "120초까지 기다리고, 그다음 로컬 대화상자로 넘긴다"는 실제로는 "로컬 대화상자는 처음부터 떠 있고,
120초 안에 원격 답이 오면 그것으로 정해지며, 아니면 로컬 대화상자가 그대로 있다"입니다. SD5의 뜻은 그대로입니다.
시간 초과나 오류로 허용되는 일은 없습니다. S4에 대한 결과 두 가지:

- 설치하는 hook 항목의 `timeout`은 120초 대기보다 커야 합니다(약 130초). 아니면 CLI가 hook을 먼저 죽입니다(30초
  `timeout`에서 프로브 hook이 조용히 죽었습니다).
- hook은 SIGTERM을 받으면 허브 요청을 거둬서, 대시보드에 지난 Allow/Deny가 남지 않게 해야 합니다.

또한 사용자 설정을 빼도 CLI는 기본으로 **auto 모드**로 시작했고, auto 모드에서는 무해한 Bash 호출에
`PermissionRequest`가 아예 오지 않았습니다. 따라서 원격 승인은 주로 manual과 accept-edits 모드에서 의미가 있습니다.
Shift+Tab은 manual → accept edits → plan → auto 순으로 돕니다.

### 2. Esc로 끊으면 `Stop`이 오지 않는다

설계는 `busy`를 `UserPromptSubmit`과 `Stop` 사이로 정의합니다. 끊긴 턴은 `Stop`을 남기지 않으므로 세션은 영원히
`busy`에 머물고, `idle`이 되지 않으며, 깨워지지도 않습니다. 데몬에는 대비책이 필요합니다. 다음 `UserPromptSubmit`이나
`SessionEnd`가 열린 턴을 닫고, 상한(예: hook 소식 없이 30분)을 넘긴 턴은 끝난 것으로 봅니다.

### 3. 빈 입력 줄에는 정확한 패턴이 있다

160열과 100열 모두에서 빈 입력 줄은 프롬프트 문자 뒤에 **줄바꿈 없는 공백**이 오는 형태입니다. 바이트로
`ESC[39m` `❯` `U+00A0`(`302 240`). 함정 두 가지:

- CLI는 그 줄에 **프롬프트 제안**을 유령 글씨로 자주 보여 줍니다(흐림, SGR 2, 예: `❯ ls -l <file>`). 입력된 글이
  아니며, 실제로 입력한 글은 흐리지 않습니다.
- 이전에 제출한 프롬프트도 기록에서 `❯`로 시작하고 회색 배경(`48;5;237`)으로 그려지므로, `❯`를 찾으면 기록도
  걸립니다.

S2 규칙: `tmux capture-pane -e -p`로 잡고, `❯`로 시작하면서 기록 배경이 아닌 **마지막** 줄을 골라, `❯` + U+00A0 뒤에
흐린(SGR 2) 부분이나 공백만 있을 때만 비어 있다고 봅니다. 그 밖의 모든 경우, 패턴을 못 찾은 경우도 포함해 wake를 하지
않습니다(fail closed). 패턴은 이 CLI 버전에 묶여 있으니 고정 버전이 바뀌면 다시 확인해야 합니다.

### 4. `client_activity`: 무엇이 바꾸는지, 클라이언트가 없을 때

- 클라이언트가 붙으면 `client_activity`는 붙은 시각이 됩니다.
- **클라이언트를 통해** 입력한 키는 값을 바꿉니다(pty로 클라이언트를 조작해 확인: 키 하나에 +8초).
- `tmux send-keys`는 값을 바꾸지 **않으므로**, 데몬이 보내는 wake 줄이 사람이 입력한 것처럼 보이지 않습니다.
- 클라이언트가 없으면 `client_activity`는 비어 있고 `session_attached`는 0입니다.

`tmux list-clients -t <session> -F '#{client_activity}'`로 읽고 **가장 큰** 값을 씁니다. `display -p`는 클라이언트 하나만
봅니다. 설계는 클라이언트가 없을 때 조건 4가 어떻게 되는지 말하지 않습니다. 제안(결정 아님): 클라이언트가 없으면
아무도 입력할 수 없으므로 조건 4는 통과, 조건 5(빈 입력 줄)는 그대로 적용.

### 5. 요약 호출 같은 자식 CLI도 hook을 부른다

hook을 보는 `claude --print` 자식(여기서는 설계가 계획한 사용자 범위 설치 대신 `--settings`로 줌)은 `SessionStart`,
`UserPromptSubmit`, `Stop`, `SessionEnd`를 불렀고, `CLAUDE_CODE_ENTRYPOINT=sdk-cli`였습니다. 사용자 범위로 설치하면
요약 호출, `scripts/zai-agent.sh` 실행, 그 밖의 스크립트 CLI가 모두 세션으로 등록되고, `Stop`에서 시작한 요약이 또 다른
요약을 시작할 수 있습니다.

본 entrypoint 값: `cli`(대화형 터미널), `sdk-cli`(`--print`), `claude-desktop`(데스크톱 앱 Code 탭). IDE 값은 확인하지
않았습니다. `CLAUDE_CODE_CHILD_SESSION=1`은 최상위 대화형 세션에서도 설정되어 있어 자식 표시가 되지 못합니다.

S2 규칙(허용 목록의 반대로, 데스크톱과 IDE 세션이 빠지지 않게): hook과 세션 MCP는 `CLAUDE_CODE_ENTRYPOINT`가
`sdk-cli`이거나 `GUARDIAN_SESSION_CHILD=1` 같은 표식이 있을 때를 **빼고** 동작합니다. 요약 자식은 그 표식을 설정하고
`~/.guardian/session/` 아래의 빈 작업 디렉터리에서 돕니다. `--setting-sources project`는 시작한 디렉터리의 프로젝트
hook을 여전히 읽기 때문입니다.

## MCP

- MCP 자식의 환경에는 `CLAUDE_CODE_SESSION_ID`와 `TMUX_PANE`이 있고 부모 pid는 CLI의 pid이므로, 세션 MCP는 데몬을
  통한 핸드셰이크 없이 hook과 같은 세션에 묶일 수 있습니다.
- CLI가 보낸 첫 메서드는 `initialize` **이전의** `server/discover`였습니다. 실제 세션 MCP는 모르는 메서드에 JSON-RPC
  "method not found"로 답하고 계속 돌아야 합니다.
- auto 모드에서는 MCP 도구가 묻지 않고 돌았습니다. **manual 모드**에서는 `session_inbox` 호출이 로컬 권한 대화상자와
  `mcp__s0stub__session_inbox`에 대한 `PermissionRequest`를 **함께** 일으켰습니다. 허용 규칙이 없으면 `Stop` 막기와
  wake마다 대화상자가 뜨고, 원격 승인은 세션 자신의 도구를 허브로 넘기게 됩니다. S2에 필요한 것:
  - 읽기 전용 세션 도구(`session_inbox`, `session_status`, `session_list`)의 허용 규칙을 설치기가 씀. 사용자 설정을
    바꾸므로 운영자에게 보여 주고 확인받음.
  - `PermissionRequest` hook은 세션 MCP 자신의 도구를 무시함.

## 그 밖의 관찰

- hook 환경에는 `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_ENV_FILE`, `CLAUDE_CODE_CHILD_NOTES_DIR`와
  메시징 소켓, 토큰 한 쌍도 있습니다(이름만, 값은 기록하지 않음).
- `Stop` 입력에 `last_assistant_message`가 있어, 요약에는 transcript 파일도 그 형식도 필요 없습니다. `background_tasks`,
  `session_crons`, `effort`도 있습니다.
- `PermissionRequest` 입력에는 `tool_name`, `tool_input`, `permission_suggestions`가 있습니다. `tool_input`의 해시로 허브
  요청을 그 한 대화상자에 묶을 수 있습니다(설계 "Authority and limits").
- 클라이언트가 붙으면 tmux는 창을 클라이언트 크기로 바꾸므로(프로브 클라이언트는 80x24) 캡처 폭은 사람의 터미널에
  따라 바뀝니다. 정정 3의 규칙은 폭에 기대지 않습니다.
- 프로브 세션은 운영자의 `~/.claude/projects/` 아래에 transcript 디렉터리 하나(버리는 디렉터리 이름)를 남겼습니다.
  프로브 프롬프트만 들어 있습니다.

## 2부: 허브 접속

### 진행 방법

버리는 키 하나와 `authorized_keys` 한 줄에 대한 운영자의 OK를 받고 했습니다. 허브에는 `~/guardian-eval/` 아래
디렉터리(모드 700)에 ed25519 키와, `ws-mac-1`의 ed25519 호스트 키를 고정한 별도 `known_hosts`를 두었습니다(워크스테이션에서
읽은 지문과 허브의 `ssh-keyscan` 결과가 같았습니다). `ws-mac-1`(OpenSSH 10.5p1, 원격 로그인)에는 `guardian-s0-probe`로
표시한 한 줄을 넣었습니다.

```
command="/usr/bin/python3 <stub> serve",restrict ssh-ed25519 <key> guardian-s0-probe
```

스텁은 argv, `SSH_ORIGINAL_COMMAND`, stdin과 stdout이 터미널인지, `SSH_TTY`, `SSH_AUTH_SOCK`, `DISPLAY`, cwd, uid, 환경
변수 이름을 기록하고, `HELLO` 줄을 찍은 뒤 stdin을 줄마다 되돌려 보냈습니다. 허브(OpenSSH 9.6p1)는 tailnet으로 `-F
/dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile=<고정본> -o GlobalKnownHostsFile=/dev/null -o
StrictHostKeyChecking=yes -o BatchMode=yes`, 즉 릴레이가 쓸 옵션으로 접속했습니다. 끝난 뒤 그 줄을 지웠고(파일은 백업과
바이트 단위로 같음), 옛 키로 접속하면 거절되는 것을 확인했고, 허브의 키 디렉터리를 지웠습니다.

### 결과

| 허브에서 시도한 것 | 결과 |
|---|---|
| 명령 없이, stdin 파이프 | **통과.** 강제 명령이 argv `serve`로 돌고, 줄이 양방향으로 흐름. 터미널, `SSH_AUTH_SOCK`, `DISPLAY` 없음. cwd는 홈 디렉터리, 종료 코드 0 |
| 클라이언트 명령(`ls /; id`) | **통과.** 대신 강제 명령이 돌고, 클라이언트의 명령은 `SSH_ORIGINAL_COMMAND`에만 나타남 |
| `-tt` | **거절**("PTY allocation request failed", 클라이언트 종료 255). **다만** 워크스테이션에서는 강제 명령이 터미널 없이 이미 시작됨 (정정 8) |
| `-A -X` | **통과.** 에이전트 소켓도 `DISPLAY`도 없음 |
| `ExitOnForwardFailure`와 `-R` | **거절**(원격 포트 포워딩 실패), 종료 255 |
| `-L` | **사실상 거절.** 클라이언트는 로컬에서 듣지만 채널마다 "administratively prohibited", 데이터가 지나가지 않음 |
| `-W` | **거절**("stdio forwarding failed"), 종료 255 |
| `sftp` | subsystem 요청이 `SSH_ORIGINAL_COMMAND=/usr/libexec/sftp-server`로 **강제 명령에 떨어짐**. sftp가 실패한 이유는 스텁의 인사말을 패킷 길이로 읽었기 때문일 뿐 (정정 6) |
| 비어 있는 고정 `known_hosts` | **거절**("Host key verification failed") |
| 줄을 지운 뒤의 옛 키 | **거절**(publickey) |

### sshd 아래의 환경

sshd는 강제 명령을 사용자의 로그인 셸로 돌리므로, 비대화형 셸의 시작 파일(이 Mac에서는 `~/.zshenv`)이 접속마다 돕니다.
환경 변수 이름은 19개로, `PATH`, `HOME`, `TMPDIR`, `SHELL`과 개발 도구가 거기서 내보낸 변수들이 있고 `LANG`은 없습니다.
`PATH`에는 사용자 자신의 도구 디렉터리와 `/usr/bin:/bin:/usr/sbin:/sbin`이 있지만 Homebrew의 `bin`은 **없어서** `tmux`를
이름으로 찾지 못합니다. `claude`는 (사용자 디렉터리에서) 찾았습니다. 부모 프로세스는 `sshd-session: <user>@notty`입니다.
보호 폴더 `~/Documents`, `~/Desktop`과 외장 볼륨을 접속에서 나열할 수 있었습니다. macOS는 이것을 원격 로그인의 전체 디스크
접근 옵션에 묶는데, 그 설정값은 읽지 않았습니다. 접속하는 동안 기본 tmux 서버가 돌고 있지 않아서, 강제 명령에서 사람의
tmux 서버에 닿는지는 보이지 않았습니다(같은 uid라 될 것으로 보며, S2에서 확인).

### 설계 정정

#### 6. `serve`는 비어 있지 않은 `SSH_ORIGINAL_COMMAND`를 거절한다

`restrict`는 포워딩, 에이전트, X11, 터미널을 막지만 클라이언트가 실행해 달라는 것은 막지 않습니다. 명령도 subsystem(sftp)도
강제 명령에 떨어지고, 요청은 `SSH_ORIGINAL_COMMAND`에 담깁니다. `serve`는 이 변수가 있고 비어 있지 않으면 아무것도 출력하지
않고 바로 끝나며, 한 줄을 기록해야 합니다(fail closed). 릴레이는 명령을 보내지 않습니다.

#### 7. 데몬은 자기 환경을 가져오고, 프로토콜은 확인하는 인사말로 시작한다

로그인 셸의 시작 파일이 `serve`보다 먼저 돌고 `PATH`를 정합니다. `command=` 줄은 인터프리터를 절대 경로로 적고(이번처럼),
데몬은 설치 때 기록한 절대 경로로 tmux와 CLI를 부르며, `LANG`과 `PATH`를 물려받지 않고 직접 정합니다. 무언가를 출력하는
시작 파일은 프로토콜 앞에 바이트를 끼워 넣으므로, `serve`는 고정된 버전 붙은 인사말로 시작하고, 첫 줄이 그것이 아니면
릴레이가 연결을 끊습니다.

#### 8. PTY를 거절해도 강제 명령은 멈추지 않는다

`-tt`에서 클라이언트는 포기했지만 `serve`는 워크스테이션에서 이미 시작됐습니다. 릴레이는 터미널을 요청하지 않고(`-T`),
`serve`는 stdin이 터미널이면 돌지 않으므로, 터미널 요청은 어떤 형태로도 데몬에 닿지 않습니다.

## 3부: 허브 쪽 링크 입구

### 진행 방법

SD1/SD2를 고친 뒤(링크는 워크스테이션이 엽니다), 운영자의 승인을 받아 `ws-mac-1`에 버리는 키 하나와 허브의
`~/.ssh/authorized_keys`에 임시 줄 하나를 두고 돌렸습니다. `guardian.py --session-link` 자리에는 `~/guardian-eval/`
아래 디렉터리(모드 700)의 프로브 스텁을 두었고, `guardian-session:ws-mac-1` 표시를 단 줄은 다음과 같았습니다.

```
from="<workstation tailnet address>",command="/usr/bin/python3 <stub> --session-link --workstation ws-mac-1",restrict ssh-ed25519 <key> guardian-session:ws-mac-1
```

스텁은 argv, `SSH_ORIGINAL_COMMAND`, stdin과 stdout이 터미널인지, 부모 프로세스, cwd, uid, 환경 변수 이름을
기록했고, `SSH_ORIGINAL_COMMAND`가 있거나 stdin이 터미널이면 거절했으며, 버전을 단 인사말 한 줄을 찍고 stdin을
되돌려 보냈습니다. `ws-mac-1`(OpenSSH 10.5p1)이 tailnet으로 `site-a-arm1`(OpenSSH 9.6p1)에
`-T -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile=<pinned> -o
GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes`로 접속했고, 고정 파일에는 허브의
ed25519 호스트 키가 있었습니다. 끝난 뒤 줄을 지웠고(파일은 백업과 바이트 단위로 같고 모드 600), 옛 키로 접속하면
거절되었으며, 허브의 스텁 디렉터리와 워크스테이션의 키를 지웠고, 남은 스텁 프로세스는 없었습니다.

### 결과

| 워크스테이션에서 시도한 것 | 결과 |
|---|---|
| `-T`, 명령 없음 | **통과.** 인사말이 첫 줄입니다(그 앞에 로그인 셸에서 나온 것이 없음). argv에 `--session-link --workstation ws-mac-1`이 들어 있고, 줄이 양쪽으로 오가며, 종료 코드 0 |
| 클라이언트 명령(`id; ls /`) | 스텁이 **거절.** 명령은 `SSH_ORIGINAL_COMMAND`로 들어왔고, 종료 코드 1, 출력 없음 |
| `-tt` | **거절**("PTY allocation request failed", 종료 코드 255). **하지만** 스텁은 허브에서 터미널 없이 시작했습니다. 부모는 이미 `systemd`였고(sshd 쪽이 사라짐), 닫힌 채널에 인사말을 쓰다 끝났습니다. 로그에 끝 줄이 없고 남은 프로세스도 없습니다(정정 9) |
| `-A -X` | **통과.** 에이전트 소켓도 `DISPLAY`도 없습니다(허브 sshd는 `X11Forwarding yes`이지만 `restrict`가 이깁니다) |
| `ExitOnForwardFailure`와 `-R` | **거절**(remote port forwarding failed), 종료 코드 255 |
| `-L` | **사실상 거절.** 모든 채널이 "administratively prohibited" |
| `-W` | **거절**("stdio forwarding failed"), 종료 코드 255 |
| `sftp` | 서브시스템 요청이 `SSH_ORIGINAL_COMMAND=/usr/lib/openssh/sftp-server`로 강제 명령에 도착, 스텁이 **거절** |
| 허브의 공인 주소로 접속 | **거절**(publickey). 줄의 `from=`이 키를 워크스테이션의 tailnet 주소에 묶습니다(아래 제안) |
| 빈 고정 `known_hosts` | **거절**("Host key verification failed") |
| `-N` | 인증된 연결은 열린 채 남지만 세션 채널이 열리지 않아 강제 명령이 돌지 않고, 스텁은 아무것도 기록하지 않았습니다 |
| `ServerAliveInterval=30`으로 100초 대기 | **통과.** 스트림이 살아 있었고 다음 줄이 되돌아왔습니다 |
| 줄을 지운 뒤 옛 키 | **거절**(publickey) |

### 허브 sshd 아래의 환경

허브 사용자의 로그인 셸은 bash이고, `.bashrc`가 비대화형 셸에서 일찍 돌아가므로 인사말 앞에 아무것도 나오지
않았습니다. 강제 명령이 본 환경 변수 이름은 16개로, `LANG`이 있고(pam_env) `PATH`는 시스템 기본값
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`)이며 사용자의
`~/.local/bin`은 없습니다. 부모는 `sshd`, cwd는 홈 디렉터리입니다. `SSH_CONNECTION`에는 워크스테이션의 tailnet
주소가 보입니다. PAM과 logind가 링크마다 세션을 엽니다(`XDG_SESSION_ID`, `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS`가 있음). 관련된 `sshd -T` 값은 `ClientAliveInterval 0`, `MaxSessions 10`,
`MaxStartups 10:30:100`, `AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2`, `PermitUserEnvironment no`,
`PasswordAuthentication no`, `AllowTcpForwarding yes`, `PermitTTY yes`이고, sshd는 `0.0.0.0:22`와 `[::]:22`에서
듣습니다. 곧 tailnet뿐 아니라 공인 주소에서도 듣습니다. 정정 6-8은 허브에서도 쓴 그대로 맞습니다. `.bashrc`의 이른
반환은 사용자가 고칠 수 있는 파일에 달려 있으니 인사말 확인은 그대로 두고, `PATH`에 사용자 디렉터리가 없으니
정정 7의 절대 경로가 필요합니다.

### 설계 정정

#### 9. 링크 입구는 핸드셰이크가 끝날 때까지 아무것도 건드리지 않는다

클라이언트 쪽에서 실패한 접속(여기서는 거절된 PTY)도 허브에서 강제 명령을 띄울 수 있고, 그런 프로세스는 첫 쓰기
때까지만 살 수 있습니다. 그래서 설계의 "워크스테이션의 새 스트림은 옛 스트림을 끝낸다"는 규칙은 인사말과
워크스테이션의 응답이 둘 다 통과한 뒤에만 적용해야 합니다. 그 전에는 입구가 저장소를 건드리지도, 기존 스트림을
끝내지도 않으므로, 끝까지 가지 못한 접속이 멀쩡한 링크를 끊을 수 없습니다. 입구는 broken pipe에서 조용히
끝납니다.

#### 10. 하트비트는 링크 프로토콜의 몫이다

허브 sshd는 keepalive를 보내지 않고(`ClientAliveInterval 0`), Guardian은 sshd를 바꾸지 않습니다. 위의 대기
스트림이 살아 있던 것은 클라이언트가 `ServerAliveInterval`을 보냈기 때문입니다. FIN 없이 절전하거나 네트워크를
잃은 워크스테이션은 TCP keepalive가 포기할 때까지, 곧 몇 시간 동안 입구를 읽기에 묶어 둡니다. 그래서 "States"의
"하트비트가 90초 실패"는 링크 프로토콜 안의 양방향 애플리케이션 하트비트입니다. 데몬은 `ServerAliveInterval`도
설정하고, 입구는 한도를 넘게 조용한 스트림을 끝냅니다. 설정값에서 읽은 것이고 절전은 돌리지 않았습니다.

### 제안: `from=`으로 키마다 tailnet 주소에 묶기 (SD9로 채택)

설계는 tailnet 피어만 허브의 SSH 포트에 닿는다고 가정하지만, sshd는 공인 주소에서도 듣고 headscale ACL은 그 길을
막지 않습니다. `from=`이 없으면 훔친 링크 키는 공인 포트에 닿는 곳 어디서나 통합니다. `from="<workstation tailnet
address>"`를 두자 공인 주소로의 접속이 거절되었습니다. 등록 때 이미 워크스테이션의 tailnet 노드를 읽으므로 주소는
손에 있고, 노드가 새 주소로 다시 등록되면 Settings가 줄을 다시 써야 합니다. 운영자가 같은 날 설계의 결정 SD9로
채택했습니다.

### 메모

- `-N` 연결은 링크 입구에 보이지 않습니다. 훔친 키로 sshd 한도까지 빈 연결을 붙잡아 둘 수 있습니다. 위험은 낮고
  지금 바꿀 것은 없습니다.
- 링크마다 허브 사용자의 logind 세션이 열립니다. 전용 Unix 사용자에 관한 열린 점과 관련이 있습니다.
- sshd는 이미 `~/.ssh/authorized_keys2`를 읽습니다. Guardian이 그 파일을 통째로 맡고(원자적 rename) 운영자의
  `authorized_keys`는 건드리지 않을 수 있으며, sshd는 바꾸지 않습니다. 배포판 기본값이라 바뀔 수 있으니 등록 때
  확인해야 합니다(`sshd -T`는 sudo가 필요하므로, 아니면 시험 접속). 열린 점 3의 선택지이지 결정이 아닙니다.

## 4부: bypass 모드

### 진행 방식

1부와 같은 Mac, 실행기, 훅 스크립트(claude 2.1.329, Haiku)로, 새 임시 디렉터리에서 `--permission-mode bypassPermissions`로
띄웠습니다. 사용자 설정은 계속 뺐으므로 운영자가 저장해 둔 bypass 동의는 적용되지 않았습니다. 동의 대화상자를 수락하지도
않았습니다: 프로브의 플래그 설정 파일(`--settings`)에 `skipDangerousModePermissionPrompt: true`를 넣었고, CLI는 플래그 설정의
이 키를 따르므로 대화상자가 뜨지 않았고 동의는 어디에도 기록되지 않았습니다. 세션은 셋입니다:

1. bypass, 규칙 없음: 일반 Bash, `.claude/`와 `.git/` 안에 Write, MCP 도구 호출 한 번. 이어서 플래그 설정에
   `permissions.ask` 규칙 하나, 훅은 기록만 하고 로컬 대화상자는 취소(Esc);
2. bypass로 시작해 세션 도중 shift+tab으로 bypass에서 나가기;
3. 프로젝트 자신의 `.claude/settings.json`(운영자가 규칙을 두는 곳)에 ask 규칙 둘을 두고, 훅이 `deny`, 이어서 `allow`로 답함.

운영자의 `~/.claude/settings.json`은 바뀌지 않았습니다(전후 해시). 1부처럼 CLI가 임시 디렉터리의 폴더 신뢰를
`~/.claude.json`에 기록하고 `~/.claude/projects/` 아래에 대화 기록을 남겼습니다. 둘 다 손으로 고치지 않고 운영자에게
남겼습니다.

### 결과

| 확인 | 결과 |
|---|---|
| bypass에서 보통의 도구 호출 | 확인 창 없음, `PermissionRequest` 없음: 일반 Bash `touch`, `.claude/`와 `.git/` 안에 Write, MCP 도구 호출(스텁이 표지를 돌려줌) |
| bypass에서 `permissions.ask` 규칙(플래그 설정) | **확인 창이 뜸.** 대화상자가 규칙과 출처("from command line arguments")를 밝히고, `PermissionRequest`가 `permission_mode: "bypassPermissions"`, `tool_name`, `tool_input`과 함께 발생 |
| 같은 종류의 규칙을 프로젝트 `.claude/settings.json`에 두고 훅이 `deny` | 훅의 메시지("Denied by PermissionRequest hook")로 명령 거부, 열린 대화상자 없음, 파일 없음 |
| 같은 조건에서 훅이 `allow` | 명령 실행, 파일 생성. 훅이 곧바로 답했으므로 정정 1의 경주에서 이김 |
| 세션 도중 bypass에서 나가기 | shift+tab은 bypass, auto, manual, accept edits, plan, bypass 순으로 돈다. manual 모드에서 일반 `touch`가 확인 창을 띄웠고 `PermissionRequest`가 `permission_mode: "default"`로 발생. 대화상자에 "Yes, and switch to auto mode" 선택지가 있었음 |
| 훅 입력 | `UserPromptSubmit`, `Stop`, `PermissionRequest`에 `permission_mode`가 있음 |
| `SessionEnd` | 모든 세션에서 `/exit`로 발생 |

### 설계 정정

#### 11. bypass 모드라고 원격 승인이 꺼지지 않는다

설계는 bypass 세션이 확인 창을 띄우지 않으므로 거기서는 `PermissionRequest`가 발생하지 않고 원격 승인이 해당되지 않는다고
적었습니다. 2.1.329에서는 둘 다 틀립니다: ask 규칙은 bypass 모드에서도 확인 창을 띄우고, 사람은 언제든 bypass에서 나갈 수
있으며, 두 경우 모두 훅이 불리고 그 답이 결정합니다. 그래서 훅은 모드에서 아무것도 가정하지 않습니다: SD11의 선택
(`GUARDIAN_REMOTE_APPROVAL=1`)이 정하고, `permission_mode`는 운영자가 참고하도록 요청과 함께 기록합니다. ask 규칙은 bypass
세션의 완화책이 됩니다(위험 "peer 메일과 자율 세션"): 운영자는 고른 명령을 확인 창 뒤에 두고 원격으로 답할 수 있습니다.
시험하지 않은 것: auto 모드, bypass에서의 `deny` 규칙.

### 메모

플래그 설정 파일로 bypass 동의를 미리 할 수 있습니다(`skipDangerousModePermissionPrompt`). Guardian의 데몬과 설치 프로그램은
그 키를 쓰지 않고 CLI를 bypass 플래그로 띄우지 않습니다. 모드는 사람이 고릅니다.

## 하지 않은 것

| 항목 | 필요한 것 |
|---|---|
| 강제 명령에서 사람의 tmux 서버에 닿기 | 접속하는 동안 돌고 있는 tmux 세션 (S2) |
| auto 모드, bypass 모드에서의 `deny` 규칙 | 원격 승인이 이것들을 다뤄야 한다면 프로브 세션 하나 |
| 워크스테이션 절전과 복귀 (그리고 조용한 링크를 허브 쪽에서 끝내기, 정정 10) | 2026-11-20 운영자가 뺌: S1과 S2가 조용하거나 죽은 스트림으로 대신 시험 |
| WSL 워크스테이션 | tailnet에 들어 있고 tmux와 CLI가 있는 WSL 호스트 (SD1/SD2를 고친 뒤로 SSH 서버는 필요 없음) |
| S0w (네이티브 Windows) | S0 이후 |

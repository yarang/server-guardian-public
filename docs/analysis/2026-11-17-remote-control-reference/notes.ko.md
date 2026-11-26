# 참고 노트: Claude Code Remote Control과 세션 간 메시징

[English](notes.md) | 한국어

[operator-sessions.ko.md](../../design/operator-sessions.ko.md)를 위해 2026-11-17 벤더의 공개 문서에서 모았다. 이 노트는
Guardian이 쓰지도 기대지도 않는 제품 기능을 설명한다. "문서에 없음"으로 적은 항목은 찾아봤지만 나오지 않은 것이다. 이 기능은
CLI 버전에 따라 바뀌므로, 세부 사항에 기대기 전에 링크를 다시 확인한다.

## 출처

| 주제 | 링크 |
|---|---|
| Remote Control(시작, 연결, 보안, 문제 해결) | https://code.claude.com/docs/en/remote-control.md |
| 세션 간 메시징(`ListAgents`, `SendMessage`, 받은편지함 소켓, 다른 장비) | https://code.claude.com/docs/en/cross-session-messaging.md |
| 인증(로그인 방법, 우선순위, 자격 증명 저장, 만료) | https://code.claude.com/docs/en/authentication.md |
| 오류 메시지(Remote Control 로그인, 갱신, 계정 변경) | https://code.claude.com/docs/en/errors.md |
| 세션(이름, `/rename`, 이어 하기) | https://code.claude.com/docs/en/sessions.md |
| 휴대폰 앱 | https://code.claude.com/docs/en/mobile.md |

[resident-agents.ko.md](../../design/resident-agents.ko.md)의 "재사용 검토"에서 이미 살펴본 관련 프로젝트:
`awslabs/cli-agent-orchestrator`, `mcp_agent_mail`, `claude-squad`, `ccmanager`, `Tmux-Orchestrator`.

## 시작 방법

- `claude remote-control`(별칭 `claude rc`): 서버 모드. 터미널에 머물며 여러 세션을 받는다(`--capacity`, 기본 32;
  `--spawn same-dir|worktree|session`).
- `claude --remote-control`(`--rc`): 원격 연결이 붙은 대화형 세션. 로컬 입력도 그대로 된다.
- 세션 안의 `/remote-control`(`/rc`): 지금 대화를 연결한다.
- 서버 모드를 처음 실행하면 `Enable Remote Control? (y/n)`을 묻고, 새 디렉터리에서는 신뢰 여부를 묻는다.
- URL이 출력되고, 서버 모드에서는 스페이스바로 QR 코드를 켜고 끈다. 세션은 claude.ai/code와 휴대폰 앱에도 이름으로 보인다.

## 인증

| 자격 증명 | Remote Control |
|---|---|
| claude.ai 로그인(`/login`, `claude auth login`) | 필수. 이 로그인에서 짧은 수명의 용도별 연결 자격 증명을 갱신한다 |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper` | 거절("requires claude.ai subscription auth") |
| `claude setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` | 모델 요청만 가능. Remote Control 세션은 만들 수 없다 |
| Bedrock, Vertex, Foundry, 게이트웨이, 다른 `ANTHROPIC_BASE_URL` | 거절. 호스트가 `api.anthropic.com`이어야 한다 |

- 환경에 API 키가 있으면 로그인보다 먼저 쓰여 Remote Control이 실패한다. `/status`에서 어느 것을 쓰는지 보인다.
- 자격 증명은 macOS Keychain, Linux와 Windows에서는 `~/.claude/.credentials.json`(권한 0600)에 둔다.
- 플랜: Pro, Max, Ultra, Team, Enterprise. Team과 Enterprise는 Owner의 스위치가 필요하고, Enterprise 사용자 지정 역할에는
  "cloud sessions"와 "Claude Code remote control"이 둘 다 있어야 한다. ZDR이나 HIPAA 구성, 또는 `disableRemoteControl`이
  설정되면 쓸 수 없다.

## 등록과 연결

- 로컬 프로세스가 벤더 API에 등록하고 바깥으로 나가는 HTTPS로 일을 폴링한다. 들어오는 포트는 열지 않는다.
- claude.ai나 앱과 오가는 메시지는 API를 거치는 스트리밍 연결로 전달된다.
- 연결 중에는 대화(메시지, 답, 도구 활동)가 벤더 서버에 저장된다. 실행과 파일 접근은 로컬에 남는다.
- 세션 id는 claude.ai/code URL에서 `/code/`와 `?` 사이 부분이다. 장비 자체를 어떻게 식별하는지는 문서에 없다.
- 서버 모드의 연결 자격 증명은 약 26시간 유효하다. 갱신하지 못하면 `Poll: Authentication failed (401)`로 끝난다.

## 이름과 찾기

- 이름 순서: `--name` / `--remote-control`이나 `/remote-control`의 인자, 다음 `/rename`, 다음 마지막 의미 있는 메시지,
  다음 자동 생성 이름(`<host>-<형용사>-<명사>`, 앞부분은 `--remote-control-session-name-prefix`로).
- 다른 세션은 `ListAgents`로 찾고 `SendMessage`로 쓴다. 사람은 `/list-agents`(`/peers`)로 같은 목록을 본다. 사람이 정한
  이름이 없는 세션은 `(unnamed session)`으로 보이고, Remote Control이 연결된 동안에는 디렉터리가 가려진다.
- 같은 장비의 세션끼리는 OS 사용자로 제한된 로컬 소켓과 세션별 토큰(`CLAUDE_CODE_MESSAGING_TOKEN`)으로 통신한다. 이 경로는
  벤더 서버를 거치지 않는다.
- 다른 장비의 세션에는 벤더 서버와 그 장비의 Remote Control 연결을 거쳐 닿는다. 연결되지 않은 보낸 쪽에는 답장 주소가 붙지
  않는다. 메시지를 읽었는지는 보낸 쪽에 알려 주지 않는다.
- 받는 세션은 `crossSessionInbound`로 정한다: `accept`, `hold`(사용자를 기다림), `refuse`.

## 권한

- 다른 세션의 메시지는 사용자의 동의가 아니다. 권한 확인 창에 답하거나, 설정이나 `CLAUDE.md`를 바꿀 수 없고, 받는 쪽의
  권한 규칙이 그대로 적용된다.
- 권한 확인 창과 질문은 답할 때까지 열려 있고 원격으로 답할 수 있다. 다른 대화창은 `dialogExpiry`(기본 5분) 뒤 기본값으로
  닫힌다.
- Trusted Devices(베타, Team과 Enterprise): 등록된 기기만, 18시간 안의 로그인이 필요하고 플랫폼 인증기로 갱신한다. 벤더는
  기기의 공개 키와 기본 메타데이터를 저장한다.

## 끊김과 만료

| 상황 | 동작 |
|---|---|
| 잠자기 뒤 네트워크 복구 | 대화형 세션은 다시 연결된다. heartbeat가 약 30분 실패하면 `/remote-control`을 다시 실행한다 |
| 서버 모드, 네트워크 약 10분 끊김 | 프로세스가 끝난다 |
| 프로세스 중지 또는 장비 재부팅 | 세션이 offline이 된다. 약 4시간 안이면 같은 디렉터리에서 이어 붙일 수 있다(`claude remote-control`, `--continue`, `--session-id`) |
| 로그인 만료 임박 | 3일 전부터 경고. 만료 뒤 Remote Control은 멈추고 저절로 복구되지 않으며 로컬 세션은 계속된다. `/login` 뒤 `/remote-control` |
| 앱 로그아웃 | 세션이 끝난다 |
| 다른 계정으로 로그인 | 연결이 끊기고, 이전 세션은 새 계정으로 옮겨지지 않는다 |
| 조직 정책으로 꺼짐 | 연결이 끊기고 세션은 보관된다 |
| 계정 설정에서 기기 삭제 | 그 자격 증명이 바로 취소된다 |

## 문서에 없음

- 연결 자격 증명의 정확한 수명("짧은 수명"이라고만 함).
- 벤더 쪽 장비와 세션 식별자의 형식.
- 세션 자신의 모드 말고 원격 요청에 어떤 권한 모드가 적용되는지.
- `/logout`이 실행 중인 원격 세션을 바로 끝내는지.
- 기본 기능의 최소 CLI 버전(기능별 최소 버전만 있음. 예: `--continue`와 `--session-id`는 v2.1.200부터).

## 운영자 fleet에서 본 것 (2026-11-16)

- Remote Control로 연결된 Windows 워크스테이션(WSL)의 세션이, 자기 메시지는 도착했는데도 다른 장비에서 보낸 첫 두 메시지를
  받지 못했다. 전송 결과는 전달이 확인되지 않았다고만 했다. 그 세션이 보낸 메시지에 대한 답장은 나중에 도착했다. SD1과
  `resident-agents.ko.md`의 최소 1회 전달이 메우려는 빈 곳이다.
- 재부팅된 워크스테이션의 세션들은 목록에서 빠졌고, 다시 시작했을 때만 돌아왔다.

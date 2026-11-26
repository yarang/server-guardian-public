# M0: 노드 한 대에서 상주 CLI 점검 (2026-11-14)

[English](results.md) | 한국어

> 2026-11-14 시점의 기록이다. CLI와 그 플래그는 릴리스마다 바뀐다.

설계: `../../design/resident-agents.ko.md`(M0 단계, "가장 어려운 조건", "신호", "드라이버", "용량"). 기계 판독용 표는
`../../../configs/resident-providers.json`이다(등록이 읽는다). 점검은 Tier-1 노드 한 대(`node-1`: 2코어, 12 GB ARM)에서
손으로 했다. `/tmp` 아래 일회용 디렉터리에서 CLI마다 격리된 설정을 쓰고, 인증 정보는 심볼릭 링크로만 걸고(복사하지
않음), `mail_inbox`에 고정 단어로 답하는 스텁 메일함 MCP 서버를 붙였다. Guardian 코드는 돌리지 않았고 허브 데이터
디렉터리에는 아무것도 쓰지 않았다. 아래의 경로, 계정, 세션 id는 자리표시자다.

## 판정

| 공급자 | 버전 | 상주 | 기본 드라이버 | 이유 |
|---|---|---|---|---|
| claude | 2.1.301 | 예 | `headless` | `tmux`는 설정 층과 도구 층을 통과했고, 입력창 층은 미확인이다(남은 점검 하나, 아래) |
| codex | 0.163.0 | **아니오** | 없음 | TUI와 `exec` 모두 도구 층에서 실패: `functions.exec`, `apply_patch`, 협업 도구가 계속 제공된다 |
| agy | 1.5.9(1.5.3에서 점검, 2026-11-15에 1.5.9로 다시 점검) | 예 | `headless` | 닫힘은 `PreToolUse` 훅 하나로 유지된다. `tmux`는 설정 층과 도구 층을 통과했고, 입력창에는 끌 수 없는 `!` bash 모드가 있다 |

따라서 D2는 claude와 agy가 `headless`, codex는 상주 불가로 정해진다. 메모리: 인스턴스당 `resident_memory_mib` 512
(D5). `max_jobs`는 바꾸지 않는다.

## 닫힘 층

1층은 격리된 설정이다(사용자 설정, 스킬, 훅, MCP 서버가 새어 들어오지 않음). 2층은 도구 집합이다. 모델에는
`mcp__mailbox__*`와 `mcp__repo__*`만 닿고, 더 약한 대체 경로는 없다. 3층은 입력창이다. 모델 출력, 훅 출력, 깨우는 줄
어느 것도 `!` 셸 입력이나 슬래시 명령을 제출할 수 없어야 한다.

| 공급자 / 드라이버 | 1층 설정 | 2층 도구 | 3층 입력창 |
|---|---|---|---|
| claude `tmux` | 통과 | 통과 | **미확인** |
| claude `headless` | 통과(기존 `llm_runner` 경로) | 통과(기존 경로) | 통과(입력창 없음) |
| codex `tmux` | 통과 | **실패** | 세지 않음 |
| codex `headless`(`exec`) | **실패**(사용자 스킬 노출) | **실패** | 통과(입력창 없음) |
| agy `headless` | 통과 | 통과(훅) | 통과(입력창 없음) |
| agy `tmux`(2026-11-15, 1.5.9) | 통과 | 통과(훅) | **미확인**(`!` bash 모드) |

### claude 2.1.301

- 실행(`env -i` 안에서 `HOME`, `PATH`, `TERM`, `LANG`, `CLAUDE_CONFIG_DIR=<config_dir>`,
  `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false`, `DISABLE_AUTOUPDATER=1`, 노드 `llm.env`의 OAuth 토큰만 넘김):

  ```
  claude --setting-sources "" --settings <settings> --tools "" --mcp-config <mcp_config> --strict-mcp-config \
    --allowedTools mcp__mailbox__mail_inbox --disable-slash-commands --permission-mode dontAsk
  ```

  `<settings>`에는 `promptSuggestionEnabled: false`와 훅이 들어 있다. 시드 파일 `<config_dir>/.claude.json`은
  `hasCompletedOnboarding`과, `<workdir>`에 대한 `hasTrustDialogAccepted`, `hasCompletedProjectOnboarding`을 켜 두어
  온보딩이나 신뢰 화면이 뜨지 않게 한다.
- 1층과 2층: 모델은 메일함 도구 하나만 나열했고 스텁의 단어로 답했다. 사용자 훅, 스킬, MCP 서버는 나타나지 않았다.
- 3층: 빈 Enter는 아무것도 제출하지 않는다. 2.1.301에는 입력창의 **`!` 셸 입력을 끄는 스위치가 없다**. 그러면 닫아야
  할 경로가 하나 남는다. 화면에 뜬 프롬프트 제안(흐린 글자)을 맨 Enter가 받아들이는 경우다. 제안은 서버 쪽 플래그로
  켜지고 원할 때 나타나지 않아서 이 점검은 돌리지 못했다. 배포된 코드를 읽어 보면 제안 게이트는
  `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION`을 먼저 보고, 플래그와 `promptSuggestionEnabled` 설정을 읽기 전에 돌아간다.
  **남은 점검:** 제안이 화면에 떠 있고 이 변수가 `false`일 때 맨 Enter가 아무것도 제출하지 않아야 한다.
  `scripts/probe-resident.sh`가 버전마다 다시 확인한다.
- `--prompt-suggestions false`는 print와 SDK 출력만 바꾼다. TUI 스위치가 아니다.
- 모델에 닿는 훅 출력: `UserPromptSubmit`의 stdout과 `Stop` 차단의 `reason`. 훅은 아무것도 출력하지 않거나 고정 문구만
  출력해야 한다. `Stop` 페이로드에는 `last_assistant_message`가 들어 있으니 전달기는 그것을 절대 되풀이하지 않는다.

### codex 0.163.0

- 시도한 TUI 플래그(`CODEX_HOME=<config_dir>`, `auth.json`은 심볼릭 링크): `--no-daemon --strict-config`,
  `CODEX_MCP_DISABLED_FEATURES`의 모든 기능과 `shell_snapshot`, `worktrees`, `sleep_tool`, `in_app_browser`,
  `realtime_conversation`, `skill_mcp_dependency_install`, `workspace_dependencies`에 대한 `--disable`,
  `-c mcp_servers.mailbox...`, `-c web_search="disabled"`, `-c notify=[...]`, `-s read-only -a never -C <workdir>`.
- 도구 목록을 물으면 모델은 메일함 도구와 함께 여전히 `functions.exec`, `apply_patch`, `collaboration.spawn_agent`와
  다른 협업 도구를 댔다. 읽기 전용 샌드박스가 `exec`가 바꿀 수 있는 것을 제한하긴 하지만, 2층은 메일함과 repo 도구만
  요구하고 더 약한 대체를 허용하지 않으므로 codex는 실패다.
- 같은 플래그의 `codex exec`도 같은 도구를 제공했고, 사용자 스킬 디렉터리의 스킬까지 나열했다. 그래서 여기서는
  설정 층도 샌다.
- TUI의 3층은 2층이 이미 실패했으므로 세지 않았다.
- **부수 발견, 2026-11-15에 정정:** 토론 엔진의 codex 잠금(`llm_runner.py`의 `_codex_mcp_args`)도 `functions.exec`를
  남긴다. 이것은 빈틈이 아니다. `exec`는 코드 모드 호스트이고 파일 API가 없으며 MCP 호출 자체가 그것을 거친다(아래
  "다시 점검과 동의, 2026-11-15").

### agy 1.5.3

- 실행(`env -i` 안에서 `HOME=<config_dir>`, `AGY_CLI_DISABLE_AUTO_UPDATE=1`):

  ```
  agy --output-format stream-json --input-format stream-json --print-timeout 200s --disable-slash-commands --print= \
    [--conversation <id>]
  ```

  임시 홈에는 노드 사용자 OAuth 토큰에 대한 심볼릭 링크, `.gemini/config/mcp_config.json`(mailbox와 repo),
  `.gemini/config/hooks.json`, 그리고 `trustedWorkspaces: [<workdir>]`와 MCP 도구별 `permissions.allow`가 든
  `.gemini/antigravity-cli/settings.json`이 있다.
- 2층은 `PreToolUse` 훅 하나에 기댄다. 내장 도구는 계속 제공되고, 훅은 `ServerName`이 `mailbox`나 `repo`인
  `call_mcp_tool`만 허용하고 나머지는 거부한다(`read_file`, `write_file`, `run_command` 등을 시도했고 모두 거부됨). 훅은
  닫힌 쪽으로 실패한다. 종료 코드 1이면 호출이 거부됐고, 멈춘 훅은 약 47초 뒤 끊기고 호출이 거부됐다. MCP 호출은
  `permissions.allow`(`mcp(mailbox/mail_inbox)`)도 있어야 하며, 없으면 agy가 거절한다.
- 3층: 헤드리스 모드에서 프롬프트는 stream-json 사용자 이벤트다. `!touch ...`는 글로 모델에 갔고, 모델이 이어서
  시도한 도구 호출은 훅이 거부했다. `/help`에는 글로 답했다.
- 2026-11-14에는 TUI를 시작하지 않았다. 온보딩 동의를 묻는데, 그때는 그 동의가 운영자의 몫이었다. 2026-11-15에 운영자가
  자동화를 허락했다(아래).

## 신호

| 신호 | claude `tmux` | codex `tmux` | agy `headless` |
|---|---|---|---|
| 턴 시작 | `UserPromptSubmit` 훅 | 없음 | 프로세스 시작. `PreInvocation`은 모델 호출마다, 한 턴에 여러 번 |
| 턴 끝 | `Stop` 훅 | `notify` `agent-turn-complete` | `result` 이벤트와 종료. `Stop` 훅(`fullyIdle`, `terminationReason`) |
| 질문 대기 | 관찰 안 됨(아래) | 없음 | 없음 |
| Stop 훅 가드 | 첫 `Stop`에서 `stop_hook_active` false, 차단 뒤 두 번째에서 true(관찰) | 해당 없음 | 해당 없음 |
| 세션 id | 모든 훅 페이로드의 `session_id` | notify 페이로드의 `thread-id`, `exec --json`의 `thread.started`의 `thread_id` | `result.conversation_id`, 훅 페이로드의 `conversationId` |
| 이어 가기 | 기존 헤드리스 경로 | 해당 없음 | `--conversation <id>`(다음 턴이 앞 답을 기억함) |

- claude의 `Notification` 훅(`notification_type: idle_prompt`)은 `Stop` 약 60초 뒤에 왔다. "턴이 끝나고 기다림"이라는
  뜻이지 질문이 아니다. `--permission-mode dontAsk`와 `--tools ""`에서는 권한 질문이 생길 수 없다. 모드에서 따라 나오는
  것이고 관찰한 것은 아니다.
- claude는 `source: startup`인 `SessionStart`도 보냈다. 훅 페이로드 키: `cwd`, `hook_event_name`, `message`,
  `notification_type`, `prompt_id`, `scratchpad_dir`, `session_id`, `transcript_path`.
- agy는 새 대화에서만 `SessionStart`를 보내고, 이어 가기에서는 보내지 않는다.
- 점검 안 함: TUI에서 claude의 `--session-id`, TUI의 시스템 프롬프트 자리(헤드리스는 `--append-system-prompt`), agy의
  시스템 프롬프트 자리. M3와 시작 카드에 필요하다.

## 메모리와 CPU

CLI마다 프로세스 트리 전체의 PSS 합(`/proc/*/smaps_rollup`)을 스텁 MCP로 짧은 턴 하나 동안 샘플링했다. 각각 표본
하나다.

| 공급자 | 대기 | 턴 중 최고 | 턴 중 CPU 비율 |
|---|---|---|---|
| claude `tmux` | 315 MiB | 331 MiB | 0.04 |
| codex `tmux` | 203 MiB | 237 MiB | 0.01 |
| agy `headless` | 해당 없음 | 256 MiB(이어 가기 233 MiB) | 0.02 |

- `resident_memory_mib`는 인스턴스당 512, 상주 슬롯의 턴에 512. 가장 높은 최고치(claude, 331 MiB)보다 약 55 % 많다.
  실제 `repo_mcp.py` 자식 프로세스와 더 긴 대화 기록을 위한 여유다.
- 노드당: 워커 작업 2 × 1536 + `headless` 턴 하나 512 = 3584 MiB. `tmux` 인스턴스 하나를 두면 512가 더해진다(4096 MiB).
  노드는 12 GB이므로 D5 예산 안에 들고 `max_jobs`는 2로 둔다.
- CPU 비율은 작다. CPU는 노드가 아니라 모델 공급자 쪽에서 쓴다.
- 이 수치는 워커 점검이 읽게 될 때까지 `configs/resident-providers.json`(`budget.resident_memory_mib`)에 둔다.
  `worker_probe.py`가 `configs/worker-requirements.json`의 모르는 키를 거부하므로, 이 키는 설치 2단계(M2/M3)와 함께
  그쪽으로 옮긴다.

## 정리

- 모든 작업은 `/tmp/<probe>` 디렉터리에서 했고 끝난 뒤 지웠다. 점검용 tmux 서버도 종료했다.
- 인증 정보는 임시 홈에 심볼릭 링크로만 걸었고 복사하지 않았다. 토큰은 출력하지 않았다.
- 잘못 쓴 곳 하나: 격리 설정을 갖추기 전의 첫 claude 실행이 노드 사용자의 `~/.claude/settings.json`에
  `{"theme":"dark"}`를 썼다(원래 `{}`). 그 뒤로 실제 설정 파일의 해시는 바뀌지 않았다. 2026-11-15에 파일이 정확히
  `{"theme":"dark"}`인지 확인한 뒤 `{}`로 되돌렸다.

## 스크립트 재확인, 2026-11-15

`scripts/probe-resident.sh node-1`(`--tui` 없이)로 프로브의 헤드리스 부분을 다시 했다. claude와 agy는 스텁 메일함 MCP를
붙여 한 턴씩 돌렸고, 프롬프트는 도구 밖에서 파일을 만들어 보라고도 시킨다. codex는 버전만 확인했고, 노드 사용자의 실제
설정 파일은 앞뒤로 해시를 비교했다.

| 프로바이더 | 버전(표) | MCP | 닫힘 | 비고 |
|---|---|---|---|---|
| claude | 2.1.301 (2.1.301) | 통과 | 통과 | |
| agy | **1.5.9** (1.5.3) | 통과 | 통과 | 훅이 도구 호출 2건을 거부 |
| codex | 0.163.0 (0.163.0) | 해당 없음 | 해당 없음 | 상주 불가 |

보호 장치는 지켜졌다(실제 설정 변화 없음). 스크립트는 종료 코드 3으로 끝난다. agy가 2026-11-14 이후 노드에서 스스로
업데이트했기 때문이다. 프로브의 실행은 `AGY_CLI_DISABLE_AUTO_UPDATE=1`을 주므로 여기서는 "고정됨"으로 읽었지만 그렇지
않았다(**2026-11-19 정정:** agy는 값 `true`만 따르므로 업데이트는 프로브 자신의 실행이 했을 가능성이 크다. 아래 "agy
1.5.12와 업데이트 스위치, 2026-11-19" 참고).
1.5.9에서도 스크립트 점검은 통과하지만, 손으로 한 부분(도구 목록, 실패·멈춤 훅, 이어 가기)은 되풀이하지 않으므로 agy를
손으로 다시 점검할 때까지 표는 1.5.3으로 둔다.

## 다시 점검과 동의, 2026-11-15

운영자가 남은 항목을 권장대로 처리하라고 했고, agy 온보딩 동의의 자동화를 허락했다.

- **동의.** agy TUI는 홈마다 한 번 약관과 데이터 사용 선택을 보여 준다. 계정은 이미 약관에 동의했으므로, 프로브는 agy가
  온보딩 뒤에 쓰는 표시 파일을 임시 홈에만 미리 둔다. `.gemini/antigravity-cli/cache/onboarding.json`에
  `consumerOnboardingComplete: true`, `enterpriseOnboardingComplete: false`, `onboardingComplete: true`. 선택 항목인
  데이터 사용 체크는 해제한 채로 두고 원격 수집도 끈다. 미리 두는 것은 평범한 온보딩에 한하며, 다른 화면이 나오면
  프로브가 멈춘다(`agy.tui_onboarding=consent_required`, 종료 코드 3).
- **agy 1.5.9 손 점검(헤드리스).** 훅은 여전히 닫힌 쪽으로 실패한다. 종료 코드 1이면 호출이 거부됐고, 멈춘 훅은 끊기고
  호출이 거부됐으며, 훅 파일이 없으면(종료 코드 127) 호출이 거부됐다. `hooks.json`이 아예 없을 때도 `run_command`는
  거부됐지만 그 상태에서 읽기와 쓰기는 시도하지 않았으므로, M3 감독자가 매번 시작 전에 `hooks.json`과 훅 파일이 있는지
  확인한다. `--conversation <id>`로 이어 가기가 되고, `SessionStart`는 새 대화에서만 발생한다. 표를 1.5.9로 올린다.
- **agy `tmux`(1.5.9).** 미리 둔 홈에서는 동의 화면이 나오지 않는다. 1층과 2층은 통과한다(TUI에서도 훅이
  `read_file`과 `write_file`을 거부하고 메일함 도구는 허용했다). 입력창에는 `PreToolUse` 훅을 거치지 않고 셸 명령을
  실행하는 `!` bash 모드가 있고 끌 스위치가 없으며, 슬래시 명령이 50개 있다(`/hooks`, `/permissions`, `/mcp`,
  `/logout` 등). 빈 입력창의 Enter는 아무것도 제출하지 않았고, 모델이나 훅 출력이 입력창으로 들어가는 경로는 보지
  못했다. claude `tmux`와 같은 부류이므로 agy의 기본값은 `headless`로 둔다.
- **버전 고정.** 모든 상주 실행은 `AGY_CLI_DISABLE_AUTO_UPDATE=1`을 주고, M3 감독자는 표에 없는 버전을 거부한다. 노드
  사용자의 agy, rc 파일, 설정은 바꾸지 않는다. (**2026-11-19 정정:** `1`은 업데이트를 끄지 못한다. 이제 값은 `true`다.)
- **claude `tmux`.** 프로브의 점검이 이제 세션을 시작한다(아래 timeout 수정). 다만 이번 실행에서는 프롬프트 제안이
  나타나지 않아 빈 Enter 점검은 판정 불가로 남고, 입력창 층도 미확인으로 남는다.
- **codex.** `functions.exec`는 코드 모드 호스트다(기능 `code_mode_host`, 0.160.0과 0.163.0에서 안정). 파일 API가 없고
  MCP 호출 자체가 그것을 거치며, 끄면 MCP가 깨진다("code-mode host is disabled"). `collaboration.spawn_agent`의 하위
  에이전트는 잠긴 도구 집합을 물려받고(셸 없음), `collaboration_modes`는 끌 수 없으며, `apply_patch`는 계속 제공되지만
  읽기 전용 샌드박스가 쓰기를 막는다. 따라서 토론 잠금에는 파일이나 셸로 가는 통로가 없고 `llm_runner.py`는 바꿀
  필요가 없다(`code_mode_host`를 켜 두는 이유를 주석으로 남겼다). 다만 메일함과 repo 밖의 도구가 계속 제공되므로 codex는
  상주 2층 기준에서 여전히 실패한다.
- **프로브 수정.** GNU `timeout`은 자식을 새 프로세스 그룹에 두기 때문에, tmux 안에서 그 아래 띄운 TUI가 SIGTTIN으로
  멈췄고 claude 점검이 `no_session_start`로 끝났다. TUI 실행은 이제 `timeout --foreground`를 쓴다.
- **스크립트 실행.** `scripts/probe-resident.sh node-1 --tui`가 종료 코드 0으로 끝났다. claude와 agy 모두 MCP와 닫힘을
  통과했고, agy는 `tui_onboarding=skipped`(동의 화면 없음), `bash_mode=present`, 빈 Enter 통과, claude의 Enter 점검은
  판정 불가(제안 표시 없음), 보호 장치는 지켜졌다.

## agy 1.5.12와 업데이트 스위치, 2026-11-19

2026-11-19 05:18 UTC부터 node-1의 agy 턴이 모두 거부됐다. 노드의 `~/.local/bin/agy`가 1.5.12가 됐는데 표는 1.5.9를
고정하고 있었다.

- **원인.** agy는 `AGY_CLI_DISABLE_AUTO_UPDATE` 값이 정확히 소문자 문자열 `true`일 때만 따른다. node-1의 일회용 홈에서
  `true`는 `auto_updater.go:247] Auto-update disabled via environment variable`을 남겼고, `1`, `TRUE`, `yes`는
  `auto_updater.go:334] Spawned background update process`를 남겼다. 따로, 마지막 확인이 15분이 안 됐으면 확인을
  건너뛰는 빠른 경로가 있다(`last_check.timestamp`). 저장소는 드라이버, 샌드박스 런처, 프로브, 표에서 모두 `1`을 줬으므로
  스위치는 1.5.9에서도 한 번도 작동하지 않았다. 상주 홈의 2026-11-15 로그와 2026-11-19 05:18:35 로그 모두
  "Language server version: 1.5.9" 아래에서 업데이트 프로세스를 띄웠다.
- **누가 업데이트했나.** 05:18:34에 시작한 상주 턴이다(agy는 05:18:29에 epoch 4로 묶였다). 상주 홈의
  `updater/update_status.json`이 05:18:40.845에 "Update successful"이고, 새 바이너리의 mtime은 05:18:40.83이다. 노드
  사용자 자신의 업데이트 상태는 2026-11-15 이후 그대로다. 노드 사용자의 평소 사용 탓으로 본 위 2026-11-15 메모는 틀렸다.
- **1.5.12 스크립트 점검**(`scripts/probe-resident.sh node-1 --provider agy --tui`, 스위치 수정 전): MCP 통과, 닫힘
  통과(훅이 호출 3개를 거부), `tui_onboarding=skipped`(미리 둔 표시 파일이 여전히 통함), `bash_mode=present`, 빈 Enter
  통과, 보호 장치 지켜짐. 버전이 바뀐 것 하나 때문에 종료 코드 3으로 끝났다.
- **1.5.12 손 점검, 헤드리스, `AGY_CLI_DISABLE_AUTO_UPDATE=true`**(사례마다 프로브 홈의 일회용 사본 하나, 프롬프트:
  `mail_inbox`를 부른 뒤 도구 밖에 파일을 만들어 보라):

  | 사례 | 결과 |
  |---|---|
  | `PreToolUse` 훅이 종료 코드 1 | 모든 도구 호출 거부(4), MCP 호출 없음, 파일 없음 |
  | 훅이 멈춤 | 끊기고 거부됨, 전체 약 165초, 파일 없음 |
  | 훅 파일 없음(종료 코드 127) | 모든 도구 호출 거부, 파일 없음 |
  | `hooks.json` 자체가 없음 | **`write_file`이 실행되어 파일이 만들어짐**, 메일함 호출도 통과 |
  | 새 대화, 실제 훅 | `call_mcp_tool` 허용, `read_file`과 `write_file` 거부, `SessionStart` 한 번 |
  | `--conversation <id>` | 이어 감(도구 없이 앞서 받은 단어를 기억), 두 번째 `SessionStart` 없음 |

  `hooks.json`이 없는 사례가 1.5.9에서 남긴 빈칸(그때는 쓰기를 시도하지 않았다)을 채운다. 닫힘 층은 훅 하나뿐이고 agy
  자체 권한은 쓰기를 막지 않는다. 드라이버는 이미 매 턴 전에 `hooks.json`(정확한 내용)과 훅 파일을 확인하므로 동작은
  바뀌지 않고, 표의 문구만 그렇게 고쳤다.
- **변경.** 스위치는 드라이버, 샌드박스 런처, 프로브, 표에서 `true`가 됐고, 고정 버전은 표, `resident_driver.py`,
  `resident_open.py`에서 1.5.12가 됐다. 프로브는 헤드리스 턴 뒤 agy 홈의 `cli-*.log`에서 업데이트 줄을 읽고
  (`agy.autoupdate=off|on|unknown`) `off`가 아니면 실패한다. 그래서 스위치가 다시 안 먹히면 예상 밖의 버전 변경이
  아니라 프로브에서 드러난다. agy가 `agy.<n>.old`로 남겨 둔 1.5.9 바이너리는 노드에 더 이상 없어서 되돌리기는
  선택지가 아니었다.
- **수정 후 스크립트 점검**(같은 명령): 종료 코드 0, 버전 1.5.12로 표와 같음, MCP 통과, 닫힘 통과(거부 2),
  `agy.autoupdate=off`, 온보딩 건너뜀, 빈 Enter 통과, 보호 장치 지켜짐.
- **배포** 2026-11-19 `293c446`: 허브 릴리스, node-1 워커를 그 커밋으로 재설치, 런처가 스위치를 정하므로 sandbox도
  재설치(sandbox 버전 `109bff91ace1`; 프로브 통과 뒤 `--record`로 다시 실행. 기록된 판정만 보류를 푼다). 배포 뒤 첫
  agy 턴(운영자 질문, 09:47 UTC)은 46초 만에 normal로 끝났고 답이 전달됐다. 그 `cli-*.log`에는 `Auto-update disabled
  via environment variable`만 있고 spawn은 없으며, agy는 그대로 1.5.12다.
- **정정(2026-11-19).** 이 기록의 이전 판은 `coder@node-1`이 epoch 11로 다시 묶였다고만 적었다. 묶이기는 했지만 턴은
  돌지 않았다. 09:34의 sandbox 재설치를 `--agent coder` 없이 실행했고, 설치기는 그 실행에 이름이 붙은 에이전트만
  `installed.json`에 썼다(`probe`, `probe2`). 그래서 워커가 09:54부터 coder의 모든 턴을 `sandbox_agent_missing`으로
  거부했다(epoch 11~21, 600초 대기). `gsa-coder` 사용자와 그 credential은 그대로였다. 11:40쯤 손으로 고쳤고(sandbox를
  `--agent coder --no-restart`로 재설치, 프로브를 `--record`로 다시 실행: 실패 0, 경고 1), 첫 normal 턴은 epoch 22,
  11:45:01~11:45:09 UTC에 돌았다. 이제 설치기는 재설치 때 기존 sandbox 에이전트를 모두 유지하고,
  `--remove --agent NAME`은 `installed.json`에서 NAME을 뺀다(`docs/design/resident-open-tools.ko.md`, 구현 메모).

## 운영자에게 남은 것

1. codex는 0.163.0에서 상주 불가다. 협업 도구와 `apply_patch`를 끌 수 있는 릴리스가 나오면 다시 점검한다.
2. claude `tmux`와 agy `tmux`는 미확인으로 남는다. 두 입력창 모두 끌 수 없는 `!` 셸이 있다. 누가 그곳에 입력할 때만
   문제가 되며, 둘 다 기본값은 `headless`다.
3. 2026-11-15 완료: agy 1.5.9 손 점검, 표 갱신, 상주 실행의 버전 고정, 온보딩 동의 자동화, 테마 파일 복구.
4. 2026-11-19 완료: agy 1.5.12 스크립트·손 점검, 업데이트 스위치 수정(`true`), 고정 버전을 1.5.12로 올림.

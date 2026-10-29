# 토론 엔진의 z.ai 프로바이더 (GLM Coding Plan) (설계)

[English](zai-provider.md) | 한국어

상태: 2026-10-23 운영자가 결정, 구현함, 2026-10-25 활성화: 키는 허브와 허브를 제외한 Tier-1 호스트 6곳에 있고, 그 6곳의 워커는 새 `llm_runner.py`를 갖고 있습니다. `ZAI_API_KEY`가 없으면 프로바이더는 준비 상태가 되지 않습니다.
배경: 운영자가 z.ai GLM Coding Plan API 키를 갖고 있고, GLM 모델을 토론 에이전트로 쓰려고 합니다.

## 결정 (운영자, 2026-10-23)

1. 키는 Coding Plan 키입니다. 약관은 지원되는 코딩 도구에서만 쓰도록 제한하는데, 엔진이 `claude` CLI를 자동으로 구동하는 것을 운영자가 감수하기로 했습니다.
2. `zai`는 자동 폴백 체인에 넣습니다.
3. 키는 허브뿐 아니라 Tier-1 워커 호스트에도 배포합니다.

## 설계

`zai`는 **claude 바이너리**를 별도 신원(z.ai의 Anthropic 호환 엔드포인트, 전용 키, 전용 설정 디렉터리)으로 실행하는 독립 프로바이더입니다. 따로 설치할 것은 없습니다.

| 항목 | 규칙 |
| --- | --- |
| 순서 | `claude, codex, agy, zai, opencode, ollama, llm`. 팀은 멤버별로 `"providers": ["zai"]`로 고를 수도 있습니다. |
| 사용 가능 | `claude` 바이너리가 PATH에 있고 `ZAI_API_KEY`가 있을 때만. claude 로그인만으로는 `zai`가 켜지지 않습니다. |
| 자격 증명 | `env_policy`: `ZAI_*`는 `zai`에만 전달되고 일반 claude는 보지 못합니다. zai 실행에는 `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`가 들어가지 않습니다. 키가 없으면 호출이 `RuntimeError`로 실패하며 claude 로그인을 빌려 쓰지 않습니다. |
| 매핑 | `ZAI_API_KEY` -> `ANTHROPIC_AUTH_TOKEN`, `ZAI_BASE_URL`(기본 `https://api.z.ai/api/anthropic`) -> `ANTHROPIC_BASE_URL`, `ZAI_MODEL`과 `ZAI_{OPUS,SONNET,HAIKU}_MODEL` -> `ANTHROPIC_DEFAULT_*_MODEL`(없으면 서버가 정함). `ZAI_*` 이름은 자식 프로세스에서 제거합니다. |
| 설정 디렉터리 | `~/.guardian/claude-zai`(`GUARDIAN_ZAI_CONFIG_DIR`), 모드 0700, `.claude.json`에 `{"hasCompletedOnboarding": true}`. 세션을 여기서 재개하므로 고정이고, 최근 claude가 환경 변수 토큰보다 `~/.claude/.credentials.json`을 우선하므로 분리합니다. |
| 세션 | claude와 같은 플래그와 파싱(`_session_claude(provider="zai")`), `--resume`, `--append-system-prompt`, repo MCP. 결과는 `zai-cli`, 실패는 `zai:`로 표시합니다. |
| tmux | `session_ops.shell_with_llm_env(command, provider="zai")`가 claude 로그인을 unset하고 `ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY"`를 셸 확장으로 export합니다. 키는 tmux 명령에 쓰이지 않습니다. |
| 감사 | `audit_run.AUDIT_PROVIDERS`는 `["claude"]` 그대로입니다. |

코드: `llm_runner.py`(`PROVIDER_BINARIES`, `zai_claude_env`, `_zai_process_config`, `_claude_family_config`), `env_policy.py`, `session_ops.py`, `debate_engine.py`(zai에도 상시 지침). 워커는 `llm_runner.py`와 `env_policy.py`를 받으므로 새 파일이 필요 없습니다. 테스트: `cli/llm-agent/test_zai_provider.py`.

## 운영

- 로컬 `~/.config/server-guardian/llm.env`(모드 600)에 `ZAI_API_KEY=...`(선택: `ZAI_BASE_URL`, `ZAI_MODEL`)를 넣고 `scripts/deploy-llm-env.sh <허브> <Tier-1 호스트>`를 실행합니다. 호스트를 명시해야 하며 키는 출력하지 않습니다.
- `scripts/lib/llm-check.sh`가 `zai` PONG 줄을 추가합니다(키가 없는 호스트는 건너뜀). `probe-worker.sh`도 이를 봅니다.

## 2026-10-25 활성화

- 운영자가 키를 교체한 뒤 직접 호출이 `glm-4.6`, `glm-4.5-air`에서 응답했고 `claude` CLI도 PONG을 받았습니다. 허브 샌드박스 확인(`~/guardian-eval/zai-verify`): PONG, `--resume`(MANGO), repo MCP 도구 호출(`repo_grep`)이 모두 통과했습니다. `ZAI_MODEL`이 없으면 claude CLI가 기본 이름 `claude-opus-5-5`를 보내고, z.ai는 GLM 모델로 답하지만(스스로 GLM이라고 답함) 응답의 모델 표기는 `claude-opus-5-5`라서 stream-json으로는 어느 GLM인지 알 수 없습니다. 모델이 중요하면 `ZAI_MODEL`을 지정하세요.
- 키는 각 호스트의 `~/.guardian/llm.env`에 합쳐 넣었습니다(다른 키 유지, 백업 `llm.env.bak-zai`). `deploy-llm-env.sh`는 파일 전체를 교체하므로 쓰지 않았습니다.
- z.ai를 향한 `claude`는 응답 앞에 안내문("auto mode ... api.z.ai isn't compatible")을 출력합니다. `llm-check.sh`는 이제 출력 전체로 판정하고 보여 줄 때만 자릅니다. 전에는 안내문이 PONG을 160자 밖으로 밀어내서 모든 호스트의 zai 줄이 실패했습니다. 호스트별 점검: 허브와 허브 외 Tier-1 6곳 모두 통과.
- 운영자가 2026-10-25에 모델을 고정했고 허브와 허브 외 Tier-1 6곳의 `llm.env`에 병합했습니다(백업 `llm.env.bak-glm`): `ZAI_OPUS_MODEL=glm-5.3`, `ZAI_SONNET_MODEL=glm-5.2`, `ZAI_HAIKU_MODEL=glm-5.3-flash`. 이 플랜은 glm-4.5~5.1과 5-turbo도 제공하며 `glm-5.3-flashx`, `glm-4.7-flash`는 거부됩니다(오류 1311, 1314). z.ai에는 단계별 think 수준이 없고 사고는 켜짐(기본) 또는 꺼짐뿐입니다.

## 열린 문제

- 허브 샌드박스 확인 2026-10-23(`~/guardian-eval/zai-verify`): 러너, 환경 격리, 키 처리는 설계대로 동작했습니다. 엔드포인트 직접 호출은 `glm-4.6`, `glm-4.5-air` 모두 HTTP 429 `[1113] Insufficient balance or no resource package`였고, `claude` CLI는 120초 제한까지 재시도했습니다. 요금제가 활성화되면 다시 돌립니다.
- z.ai의 429에서 `claude`가 얼마나 재시도하는지 측정해야 합니다. 할당량이 소진된 `zai`가 폴백 체인에 있으면 다음 프로바이더로 넘어가기 전에 타임아웃 한 번을 통째로 씁니다(`zai`에만 짧은 타임아웃을 두는 방안).

- 허브 샌드박스 `~/guardian-eval`에서 검증(운영 데이터는 건드리지 않음): PONG이 GLM 모델을 보이는지, repo MCP를 붙인 세션 턴이 `--tools ""` 아래에서 실제로 `mcp__repo__*` 도구를 호출하는지, 새 설정 디렉터리에 마커 파일 외의 온보딩 절차가 필요 없는지.
- 자동 사용에 대한 Coding Plan 약관, 폴백 체인이 `zai`에 닿았을 때의 속도 제한.

## 구현 메모

설계대로 구현했습니다. `installed_providers`는 키가 있을 때만 `zai`를 포함하므로, 바이너리는 같아도 `claude`와 `zai`는 독립 항목입니다. 배포하지 않았습니다.

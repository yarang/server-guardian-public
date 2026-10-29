# 스레드 변경 게시

[English](publish-thread.md) | 한국어

실행된 파일 액션(`jev_mcp:execute_code_action`, `jev_mcp:apply_patch`)는 허브에 있는 프로젝트 작업 클론에 **커밋되지 않은**
변경을 남긴다. 게시 단계는 운영자가 diff를 손으로 옮기지 않고 바로 그 변경만 브랜치로 프로젝트 저장소에 올리는 방법이다.
예전 판정자 도구 `jev_mcp:git_commit_push`(로컬 브랜치와 커밋, push 없음)는 폐지됐다. 도구 카탈로그, 내장 팀, 판정자
페르소나에서 빠졌고, 그 도구를 가리키는 저장된 PROPOSED 액션는 INVALID로 거부된다("git_commit_push was retired; use
publish"). 기존 행은 여전히 목록과 상세에 나온다. 의사결정 DB를 열 때 그 도구를 가리키는 미결(PROPOSED 또는 HELD) 행을
INVALID로 정리하므로 그 스레드의 게시를 더 이상 막지 않는다. `allowed_tools`에 아직 그 이름이 있는 팀은 경고와 함께
받아들인다(이름은 무시한다).

2026-10-16부터 파일 액션는 jev 게이트를 통과하면 승인 탭 없이 적용된다(`decided_by` `auto:engine`,
`debate-agent-retrieval.ko.md`의 "구현 메모 (코드 변경 자동 승인)" 참고). 그래서 게시는 변경이 저장소에 닿기 전에 사람이
거치는 마지막 단계다.

**스레드당 게시 한 번(결정 D11).** 스레드는 많아야 한 번 게시한다. 게시가 pushed로 기록된 뒤 같은 스레드를 다시 게시하면
거부한다. 더 바꾸려면 새 스레드가 필요하다. 실패한 시도는 세지 않으며 다시 시도할 수 있다.
`guardian.py --publications THREAD --json`이 기록된 시도를 보여 준다(아래 참고).

게시된 스레드는 끝난 것이다. 이어 가기를 처음부터 거부한다. `guardian.py
--debate TASK --thread T`는 `thread T was published as branch B; start a new thread`와 함께 종료 코드 4로 끝나고,
그 `thread_id`로 `POST /api/debates`를 부르면 같은 문구로 409를 답하고 작업을 시작하지 않는다(허브는 캐시된 게시 집합을
먼저 보고, 그다음 브랜치 이름을 `--publications`에 묻는다). `guardian.py --published-threads --json`은
`{"ok": true, "threads": [ids]}`를 돌려준다. `GET /api/debates` 항목은 그런 스레드에 `"thread_published": true`를 담는다.
이 값은 30초 캐시에서 오며, 게시 경로 호출이 성공하면 캐시를 비운다.

코드: `cli/llm-agent/publish_ops.py`. 진입점: `guardian.py --publish-thread THREAD [--branch NAME]`
(허브 형식은 `--yes --json --actor A`를 더한다)과 선택 본문 `{"branch": "..."}`를 받는
`POST /api/threads/{thread}/publish`. 대시보드는 토론 결과의 액션 목록에 **Publish** 버튼을 보여 준다.

## 흐름

1. 모든 전제 조건(아래)을 확인한다. 거부는 아무것도 바꾸지 않는다. 브랜치도, 커밋도, DB 행도 없다.
2. 운영자에게 확인을 받고(CLI 질문 또는 대시보드 확인 창) 프로젝트 테스트를 실행한다.
3. `thread_publications`에 시도를 기록한다(`RUNNING`).
4. `git checkout -b <branch>`, `git add -- <the exact files>`, `git commit`(제목 `fix: <decision summary>`,
   72자까지, 본문은 요약, 트레일러 `Debate-Thread: <thread>`).
5. `git push -- <repo> <branch>:refs/heads/<branch>`: force 없음, upstream 설정 없음.
6. `git checkout <tracked branch>`로 작업 공간을 다시 깨끗하게 한다. 변경은 이제 브랜치에만 있다.
7. 시도를 `PUSHED` 또는 `FAILED`(stderr 포함)로 표시한다.

## 전제 조건

모두 맞아야 한다. 아니면 허브 형식(`--yes --json`)은 이유와 함께 종료 코드 4(HTTP 409)로 끝난다(잘못된 브랜치 이름은
2 / 400, 모르는 스레드는 3 / 404). 대화형 형식(`--json` 없이)도 같은 2 / 3 / 4 코드를 쓴다. 커밋을 만든 뒤 push가 실패한
경우에만 1로 끝난다(JSON 형식은 이를 `ok: false`, 종료 코드 0으로 알린다).

- 스레드가 이미 게시되지 않았다(이전 시도가 `PUSHED`가 아니다).
- `EXECUTED` 파일 액션가 하나 이상 있고, `PROPOSED`, `HELD`, `RUNNING` 액션가 없다. `FAILED`, `REJECTED`,
  `INVALID` 액션는 막지 않지만, 그것들이 작업 공간에 남긴 것은 아래 경로 검사에 드러난다.
- 모든 파일 액션가 한 프로젝트와 비어 있지 않은 한 `base_sha`를 공유하고, 각각 `file_hashes`를 기록했다. 코드 맥락 없이
  제안된 파일 액션는 제안 시점의 작업 공간 HEAD를 `base_sha`로 기록하므로 게시할 수 있고, 실행 시점의 오래된 기준 검사도
  똑같이 적용된다.
- 작업 공간은 git 저장소의 최상위이고(실행 중인 Server Guardian 체크아웃이 아님), 프로젝트가 추적하는 브랜치에 있으며
  (detached 아님), `HEAD == base_sha`다.
- `git status --porcelain`이 보고하는 경로(추적하지 않는 파일 포함)가 액션들이 쓴 파일의 합집합과 같고, 스테이징된 것이
  없으며, 각 파일의 내용 해시가 그 파일에 마지막으로 기록된 해시와 같다(형제 액션와 같은 순서 규칙: `applied_at_ns`, 그다음 id).
- 프로젝트에 `repo` URL이 있다. 내장 server-guardian 프로젝트(`configs/projects/server-guardian.json`)는
  `"repo": "${GUARDIAN_SELF_REPO}"`로 둔다. URL은 허브의 환경에서 오므로 git에 있는 프로젝트 파일에는 계정 이름이 없다.
  `scripts/install-agent-service.sh`가 배포하는 체크아웃의 `origin` URL로 `~/.guardian/agent.env`에 `GUARDIAN_SELF_REPO`를
  써 넣는다. 변수가 없거나 비어 있으면 프로젝트에 repo URL이 없고(작업 공간은 그대로 있다) 게시는
  `project server-guardian has no repo URL to push to`로 거부된다.
- 브랜치: 기본은 `guardian/<thread>`. 지정한 이름은 브랜치 검증
  (`MCPClient._check_branch`: 패턴, `..` 없음, `.lock`이나 끝의 `/` 없음, `main`/`master`/`develop`/`release` 아님)을 통과하고,
  프로젝트가 추적하는 브랜치가 아니며, `git check-ref-format --branch`가 받아들여야 한다.
- 프로젝트가 기본 테스트(`tests.default`)를 지정한다. 없으면 게시는 "set tests.default for project X"로 거부한다(대체
  명령은 없다). 테스트는 `execute_code_action(run_test)`도 실행하는 도우미 `MCPClient.run_default_test`로 돈다. 같은 허용
  목록 검사, `run_tests`와 같은 Go 모듈 작업 디렉터리, 같은 600초 제한이다. 작업 공간에서 통과해야 하고 바뀐 경로를 그대로
  두어야 한다. 실패하면 거부에 그 출력(`tests.stdout`, `tests.stderr`)이 담긴다. 이 검사는 다른 가벼운 전제 조건과 함께,
  확인 전에 한다. 테스트 실행 자체만 확인 뒤에 온다.
  `tests.default`는 명령 문자열 하나이거나 명령 문자열 1..5개의 목록이며, 각각 단일 형식처럼 검증된다. 목록은 같은 도우미로
  차례로 실행되고, Go 단계는 모두 자기 모듈 디렉터리에서 돌며, 모든 단계가 600초 예산 하나를 나눠 쓴다. 처음 실패한 단계에서
  멈추고, 거부와 `tests.stderr`가 그 단계를 밝힌다("step 2/3 failed: ..."). 통과하면 단계별 요약(`tests.steps`)을 알린다.
  server-guardian 프로젝트는 세 단계로 막는다: Python unittest 묶음, `go vet ./apps/agent/...`,
  `go test -race ./apps/agent/...`. `-race`는 cgo와 C 컴파일러가 필요하다. 허브에는 gcc가 있고(2026-10-12 확인), 없는
  노드는 race 검출기를 건너뛰지 않고 게이트에서 눈에 띄게 실패한다. 웹과 셸 검사는 여전히 개발용 기기의 `scripts/ci.sh`와
  GitHub CI에서만 돈다. 허용 목록은 `go vet`(`-v`와 패키지 경로만) 말고는 그대로다. 셸과 경로가 붙은 스크립트는 없다.
- 이 작업 공간에서 다른 게시가 실행 중이지 않다(`.git/guardian-publish.lock`의 `flock`).

## 미리 보기(plan)

`guardian.py --publish-thread THREAD --plan --json [--branch NAME]`과 `GET /api/threads/{thread}/publish-plan?branch=NAME`은
위 전제 조건을 테스트 게이트 직전까지(게이트는 빼고) 모두 실행하며 아무것도 바꾸지 않는다(git 쓰기, DB 행, 확인 없음).
성공: `{"ok": true, "plan": {"thread_id", "project_id", "branch", "remote", "base_sha",
"files", "subject", "retry"}}`. 실제 게시도 같은 필드를 돌려준다. 거부: 종료 코드 4 / HTTP 409
`{"ok": false, "error"}`, 잘못된 브랜치 2 / 400, 모르는 스레드 3 / 404. 대시보드는 확인 창 전에 이것을 보여 준다.
미리 보기는 중단된 이전 게시를 이미 복구된 것으로 알리지만 실제로 복구하지는 않는다.
미리 보기는 작업 공간 잠금을 잡지 않는다(대화 상자를 연다고 실제 게시나 동기화가 busy로 실패하면 안 된다). `RUNNING`
행이 있으면 게시가 실행 중일 수 있다는 `note`를 더한다.

## 실패와 재시도

- 중간에 죽은 게시(허브 제한 시간, 재시작)는 `RUNNING` 행을 남기고, 작업 공간이 그 브랜치에 남아 있을 수 있다.
  다음 게시는 작업 공간 잠금 아래에서 그 스레드의 `RUNNING` 행을 `FAILED`("interrupted")로 표시하고, 작업 트리가
  깨끗하며 HEAD가 그런 행이 가리키는 브랜치에 있으면 추적 브랜치를 체크아웃한다. 남은 커밋은 재시도로 push된다.
  프로젝트 동기화도 같은 잠금을 잡고, 추적하지 않는 파일이 있는 작업 공간은 건너뛴다.
- `git add`나 `git commit`이 실패하면 스테이징을 풀고 추적 브랜치로 돌아가 새 브랜치를 지운다. 변경은 다시 그냥
  커밋되지 않은 파일이 된다.
- push가 실패하면 로컬 브랜치와 그 커밋을 남기고 추적 브랜치를 다시 체크아웃한 뒤 stderr와 함께
  `status: FAILED`를 돌려준다(HTTP 200, `ok: false`). 시도 행은 커밋 sha와 함께 `FAILED`다.
- 같은 브랜치 이름으로 게시를 다시 실행하면 테스트를 다시 돌리지 않고 기존 커밋을 push한다. 단, 그 브랜치가 여전히
  `base_sha` 위의 커밋 하나이고, 액션 파일만 기록된 내용 그대로 담고 있으며, 그 스레드와 브랜치의 `PUSHED`가 아닌 시도가
  그 커밋을 가리켜야 한다. 그 밖에는 거부한다.
- 다른 이력을 가진 원격 브랜치가 이미 있으면 push는 non-fast-forward로 실패한다. push는 절대 force하지 않는다.

## 안전 규칙

- 액션들의 정확한 파일만 add한다. `git add -A`는 쓰지 않는다.
- git은 셸 없이, 호출마다 제한 시간(60초, push 300초, 테스트 600초)과 `MCPClient.tool_environ()`
  (`env_policy.tool_env`: 토큰 없음, `GUARDIAN_*` 없음, `GIT_DIR` 같은 경로 전환 제거, `GIT_TERMINAL_PROMPT=0`)으로 실행한다.
- 자격 증명은 허브의 git credential helper에서 온다. URL에는 아무것도 넣지 않고, git stderr의 URL은 저장하거나 돌려주기
  전에 지운다.
- push 대상은 프로젝트의 `repo`다(`${GUARDIAN_SELF_REPO}`를 해석한 뒤 검증한 https URL). 테스트만 `publish_thread`에 다른
  `remote`를 넘긴다. CLI와 허브는 그것을 받지 않는다.
- 허브 경로는 스레드 id와 브랜치 이름을 검증하고, 본문을 `http.MaxBytesReader`(1 KiB)로 읽으며, 값을 CLI에 argv로만
  넘기고, 액션 실행처럼 `GUARDIAN_UI_ACTIONS=off`로 막힌다.

## 데이터

`thread_publications(id, thread_id, project_id, branch, commit_sha, remote, status, error, actor, created_at)`,
`db_manager.py`가 만든다(`CREATE TABLE IF NOT EXISTS`). `status`는 `RUNNING`, `PUSHED`, `FAILED`다. 거부는 부작용이 없으므로
기록하지 않는다. 스레드의 시도 목록은 `DecisionDBManager.list_publications`로, API로는
`guardian.py --publications THREAD --json` / `GET /api/threads/{thread}/publications`로 본다(최신 순.
debate-teams-api.md 9절 참고).

## 게시 뒤

허브 작업 공간은 `base_sha`의 추적 브랜치로 돌아와 있다. push된 브랜치는 저장소에서 평소처럼 병합한다.
server-guardian 작업 공간은 허브 체크아웃 `~/server-guardian`에서 클론했으므로, `--project-sync`는 그 체크아웃이 움직인
뒤에야 병합을 본다(허브를 다시 배포하거나 `git -C ~/server-guardian pull --ff-only`). 그다음 `--project-sync`로 작업
공간을 fast-forward한다. 새 코드에 대한 토론은 병합된 커밋에서 시작한다.

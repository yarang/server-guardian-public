# 토론 팀 UI 연동 계약 (Python CLI ↔ Go hub API ↔ 대시보드)

[English](debate-teams-api.md) | 한국어

대시보드는 Go agent(허브)의 `/api/*`만 호출한다. Go agent는 토론 엔진을 직접 구현하지 않고
`guardian.py`를 셸 없이 인자 배열로 실행한다. 검증 규칙은 Python 한 곳에만 있다.

## 1. Python CLI (Go가 호출)

작업 디렉터리: 저장소 루트 (허브 systemd `WorkingDirectory=/home/ubuntu/server-guardian`).
실행: `python3 cli/llm-agent/guardian.py <옵션>`. stdout은 JSON 한 개.

| 옵션 | stdin | 성공 (exit 0) | 검증 실패 (exit 2) |
|---|---|---|---|
| `--personas --json` | - | `{"personas":[{...persona, "source":"builtin"\|"custom"}], "custom_dir":"..."}` | - |
| `--persona-save --json` | persona JSON | `{"ok":true,"id":"...","warnings":[]}` | `{"ok":false,"error":"..."}` |
| `--persona-delete ID --json` | - | `{"ok":true,"id":"..."}` | builtin이면 exit 2, 없는 id면 exit 3 (`{"ok":false,"error":"..."}`) |
| `--teams --json` | - | 아래 참고 | - |
| `--team-save --json` | team JSON | `{"ok":true,"id":"...","warnings":[...]}` | `{"ok":false,"error":"..."}` |
| `--team-delete ID --json` | - | `{"ok":true,"id":"..."}` | builtin이면 exit 2, 없는 id면 exit 3 |

`--teams --json`:
```json
{
  "custom_dir": "/home/ubuntu/.guardian/data/teams",
  "teams": [
    {"id": "cross-review", "name": "...", "source": "builtin|custom",
     "data": { /* 팀 파일 원본 (편집기 초기값) */ },
     "lineup": { /* 정규화된 라인업, 유효할 때만 */ },
     "errors": [], "warnings": []}
  ],
  "catalog": {
    "providers": ["claude", "codex", "agy", "opencode", "ollama", "llm"],
    "installed": ["claude", "codex", "agy"],
    "tools": [{"name": "jev_mcp:run_tests", "risk": "low", "description": "..."}],
    "max_critics": 4, "max_proposers": 2, "max_rounds": 10,
    "categories": ["development", "operations", "design-docs", "general"]
  }
}
```

저장 위치: 저장소의 `configs/personas`, `configs/teams`는 기본 제공(builtin, 읽기 전용).
UI에서 저장한 것은 `~/.guardian/data/personas`, `~/.guardian/data/teams`(custom)에 쓰고, 같은 id면 custom이 우선한다.
custom을 삭제하면 같은 id의 builtin이 다시 보인다. builtin은 삭제할 수 없다(exit 2 → 400). 페르소나·팀·프로젝트·노드 그룹 모두
어느 쪽에도 없는 id의 삭제는 exit 3 → 404다.

(2026-10-19: 팀 파일에 `category`(`catalog.categories` 중 하나, 아니면 `--team-save`가 exit 2)와 엔진이 찍는 `created`·`modified`를 둘 수 있다. [debate-teams.ko.md](debate-teams.ko.md)의 "팀 파일 스키마" 참고.)

토론 실행:
```
guardian.py --debate TASK [--team ID] [--thread THREAD_ID] [--rounds N] --result-file PATH
```
(2026-10-18: Go는 이제 `--project=ID`(요청에 `project`가 있을 때)와 `--events-file=<작업 디렉터리>/events.jsonl`도 넘긴다. 이벤트 계약은 [debate-agent-retrieval.ko.md](debate-agent-retrieval.ko.md). `--team`이 없으면 팀은 스레드에 저장된 라인업 > 프로젝트의 `"team"` > `default` 순서로 정해진다([debate-teams.ko.md](debate-teams.ko.md) "우선순위").)
- stdout/stderr: 사람이 읽는 진행 로그 (Go가 파일로 저장해 UI에 보여줌)
- 끝나면 PATH에 결과 JSON (`--debate --json`과 같은 구조: thread_id, team, consensus_reached, final_decision, transcript, log_file)
- exit 0: 토론 완료 (판정 결과와 무관), exit 2: 팀 검증 실패, 그 외: 오류

## 2. Go hub API

모두 기존 `withAuth` 토큰 인증 아래(`main.go`의 전역 `withSecurity(withAuth(mux))`). `guardian.py`가 없는 노드는 `503 {"error":"debate engine is not available on this host"}`(`h.guard`가 붙은 경로; `h.guard`는 CLI가 있는지만 확인한다).

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/api/personas` | CLI 출력 그대로 |
| POST | `/api/personas` | 본문(최대 64KB)을 `--persona-save` stdin으로. 성공 200, exit 2는 400 + `{"error"}` |
| DELETE | `/api/personas/{id}` | `--persona-delete` |
| GET | `/api/teams` | CLI 출력 그대로 |
| POST | `/api/teams` | `--team-save` |
| DELETE | `/api/teams/{id}` | `--team-delete` |
| POST | `/api/debates` | `{"task", "team"?, "thread_id"?, "rounds"?}` → 202 작업 요약. 같은 `thread_id`로 실행 중인 작업이 있으면 409, 동시 실행 상한 초과 시 429 (둘 다 같은 락 안에서 검사) |
| GET | `/api/debates` | `{"debates":[작업 요약, 최신순]}` |
| GET | `/api/debates/{id}` | 작업 요약 + `"log"`(마지막 64KB) + `"result"`(완료 시 결과 JSON, 아니면 null) |
| DELETE | `/api/debates/{id}` | 실행 중이면 취소 (프로세스 그룹 종료) |

작업 요약: `{"id","task","team","thread_id","rounds","status","started_at","finished_at","exit_code","error"}`.
(2026-10-18: 요약에는 이제 `project`, `context_paths`, `context_ref`, `thread_closed`, `thread_published`, `kind`(코드 위키 빌드 작업이면 `"code_wiki"`)가 값이 있을 때 붙는다. 목록 항목에는 `events`(이벤트 스트림 유무)와 `retrieval`(결과의 코드 읽기 합계)이, 상세에는 `retrieval`이 더 있다. `POST /api/debates` 본문은 선택 필드 `"project"`(team과 같은 id 패턴)도 받는다. 없으면 기본 프로젝트 `server-guardian`이다.)
`status`: `running` · `completed` · `failed` · `cancelled` · `interrupted`(agent 재시작으로 끊김).
`thread_id`는 이어가기 요청이면 요청 값, 새 토론이면 완료 후 결과의 thread_id.

설정(환경변수): `GUARDIAN_CLI`(기본 `cli/llm-agent/guardian.py`), `GUARDIAN_PYTHON`(기본 `python3`),
`GUARDIAN_DATA_DIR`(기본 `~/.guardian/data`, 작업 기록은 `<DATA>/debate-jobs/<id>/`),
`GUARDIAN_MAX_DEBATES`(기본 2), `GUARDIAN_DEBATE_TIMEOUT`(초, 기본 3600).

입력 검증: task 1~4000자, team `^[a-z0-9][a-z0-9_-]{0,63}$`, thread_id `^thread_[A-Za-z0-9_]{1,80}$`, rounds 1~10, `{id}`는 team과 같은 패턴.
(2026-10-18: 토론 작업의 `{id}`(`/api/debates/{id}`)는 `^dbt_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$`이다. 요청 본문 상한은 페르소나·팀 64KB, 토론 16KB다.)

이 표 밖의 경로(2026-10-18): `debate_handler.go`는 프로젝트(`GET`/`POST /api/projects`, `DELETE /api/projects/{id}`, `POST /api/projects/{id}/sync`), 노드 그룹(`/api/groups`, [node-groups.ko.md](node-groups.ko.md)), 이벤트·프롬프트·결과 문서(`/api/debates/{id}/events`, `/api/debates/{id}/prompts/{seq}`, `/api/threads/{thread}/report`, [debate-agent-retrieval.ko.md](debate-agent-retrieval.ko.md))도 등록한다. 파일 보기와 코드 위키 경로는 `file_handler.go`, `wiki_handler.go`에 있다([code-wiki-coverage.ko.md](code-wiki-coverage.ko.md)).

## 3. 대시보드

- API 실패 시 가짜 데이터로 대체하지 않고 오류를 표시한다.
- 페르소나: 목록(builtin/custom 표시), 편집·저장·custom 삭제
- 팀: 목록(검증 오류·경고 표시), 편집기(역할별 페르소나, CLI 순서, 비평가 1~4, 추가 제안자 0~2(선택), max_rounds, 도구 체크박스와 위험도), 저장·custom 삭제
- 토론: 팀 선택, 작업 입력, 선택적으로 이어갈 스레드 ID → 시작 → 3초마다 조회해 진행 로그 표시 → 완료 시 발언 기록·판정·jev 결과·제안된 액션 표시, 취소 버튼, 최근 작업 목록
- (2026-10-18: 대시보드는 기본이 한국어이고 영어도 고를 수 있다([dashboard-i18n.ko.md](dashboard-i18n.ko.md)). 이 문서에 나오는 "Proposed actions", "Close thread" 같은 화면 이름은 영어 라벨이다. 토론 폼의 팀 선택을 비우면 위 우선순위대로 팀이 정해진다.)

## 4. 판정자 액션 승인 (3차)

UI 승인도 CLI와 같은 `action_ops.run_action` 경로를 쓴다. 실행 가능한 것은 `PROPOSED`뿐이고, 실행 직전 재검증하며,
DB의 `PROPOSED → RUNNING` 원자적 전이로 이중 실행을 막는다. `decided_by`에는 승인자(`dashboard@<클라이언트 IP>`)를 남긴다.

### Python CLI

종료 코드: 0 성공, 2 입력 오류, 3 없음, 4 상태 충돌(실행할 수 없는 상태, 다른 실행이 선점, 재검증 실패).
오류일 때 stdout은 `{"ok":false,"error":"..."}`.

| 옵션 | 성공 출력 |
|---|---|
| `--actions [THREAD] --json` | `{"actions":[action...]}` (최신순) |
| `--show-action N --json` | `{"action": action}` |
| `--run-action N --yes --json --actor A` | `{"ok": <EXECUTED이면 true>, "action": action, "result": {...}}` (도구가 실패해도 exit 0, `action.status`가 `FAILED`) |
| `--reject-action N --json --actor A` | `{"ok":true,"action": action}` |
| `--jev-recheck THREAD --json` | `{"ok":true,"gate":"PASSED\|DISPUTED\|PENDING","released":[id...],"review":{...}}` |
| `--publish-thread THREAD [--branch NAME] --yes --json --actor A` | `{"ok": <PUSHED이면 true>, "status","branch","commit_sha","files",...}` (push가 실패해도 exit 0, `status` `FAILED`; 전제 조건 위반은 exit 4). [publish-thread.ko.md](publish-thread.ko.md) |

`--publish-thread THREAD --plan --json [--branch NAME]`: 드라이런. 테스트 관문 앞의 모든 전제 조건을 확인하고 git/DB에는 아무것도 쓰지 않는다(`--yes` 불필요).
성공 exit 0 `{"ok":true,"plan":{"thread_id","project_id","branch","remote","base_sha","files":[...],"subject","retry"}}`,
전제 조건 위반 exit 4 `{"ok":false,"error"}`, 잘못된 브랜치 exit 2, 없는 스레드 exit 3.

`action`: `{"id","thread_id","round_num","tool","arguments","reason","status","result","decided_by","created_at","updated_at","risk","tool_description"}`.
`status`: `PROPOSED` · `HELD`(jev 관문 대기) · `RUNNING` · `EXECUTED` · `FAILED` · `REJECTED` · `INVALID`.
(2026-10-18: ops 프로젝트의 정리 액션에는 `STALE`(실행 직전 drift 검사에 걸림) 상태와 제안 시점 측정 `preview`가 더 있고, 카탈로그 액션의 `risk`는 그 액션의 인자로 계산한다. [server-ops-project.ko.md](server-ops-project.ko.md) 14장 P3.)
`--actor`: `^[A-Za-z0-9@:._-]{1,64}$`.

### Go hub API

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/api/actions?thread=THREAD` | `--actions [THREAD] --json` (2026-10-18: `project=ID`로도 거른다 → `--project=ID`) |
| GET | `/api/actions/{id}` | `--show-action` |
| POST | `/api/actions/{id}/run` | 본문 `{"confirm_id": <id>}`가 경로 id와 같아야 함. 동기 실행(요청이 끊겨도 계속 실행, `GUARDIAN_ACTION_TIMEOUT` 기본 900초). `GUARDIAN_UI_ACTIONS=off`이면 403 |
| POST | `/api/actions/{id}/reject` | `--reject-action`. `GUARDIAN_UI_ACTIONS=off`이면 403 |
| POST | `/api/actions/{id}/fail-stale` | 본문 `{"confirm_id": <id>}`가 경로 id와 같아야 함(아니면 400). 이 허브 프로세스가 그 액션를 실행 중이면 409(실행이 아직 살아 있음). 그 외에는 `--fail-stale-action ID --json --actor A` → 200 `{"ok":true,"action":{...}}` / 404(없음, exit 3) / 409(RUNNING 아님, exit 4). RUNNING을 FAILED로 옮기며 `marked_failed_by/at`을 기록하고 `decided_by`는 건드리지 않는다. 늦게 끝난 실행 결과는 `late_result`로 보존. `GUARDIAN_UI_ACTIONS=off`이면 403 |
| POST | `/api/threads/{thread}/jev-recheck` | `--jev-recheck` (타임아웃 300초). `GUARDIAN_UI_ACTIONS=off`이면 403 |
| POST | `/api/threads/{thread}/publish` | 선택 본문 `{"branch": "..."}` → `--publish-thread` (타임아웃 1200초). `GUARDIAN_UI_ACTIONS=off`이면 403 |

| GET | `/api/threads/{thread}/publish-plan?branch=NAME` | `--publish-thread --plan --json` (읽기 전용, `branch` 선택·같은 패턴 검증). `GUARDIAN_UI_ACTIONS=off`여도 허용. 200 `{"ok":true,"plan":{...}}` / 400 / 404 / 409 `{"error"}` |

종료 코드 매핑: 0→200, 2→400, 3→404, 4→409, 그 외→500. `{id}`는 양의 정수, `{thread}`는 `^thread_[A-Za-z0-9_]{1,80}$`.
`--actor`는 Go가 `dashboard@<RemoteAddr의 host>`로 만든다(사용자 입력 아님).

### 대시보드

- 토론 결과의 액션 목록과 별도의 "Proposed actions" 영역(스레드 필터, 상태 배지).
- 액션 상세: 도구, 위험도, 이유, 인자 전체(`execute_code_action`은 파일 내용 전체), 이전 결과.
- `PROPOSED`: 실행·거부. 실행 전 확인 창에서 인자를 다시 보여 주고, 고위험(`server_remediation`)은 액션 번호를 직접 입력해야 실행 버튼이 활성화된다.
- `HELD`: "jev 재평가" 버튼(스레드 단위). 통과하면 `PROPOSED`로 바뀐다.
- 실행 중에는 버튼을 잠그고, 끝나면 결과(stdout/stderr/exit code 등)를 보여 준다.

## 5. 코드 묶음 (토론 에이전트의 코드 읽기)

설계: [debate-code-context.ko.md](debate-code-context.ko.md). 작업 클론(`GUARDIAN_WORKSPACE`)의 **커밋된** 파일을 Guardian이 꺼내 프롬프트에 넣는다.

### Python CLI

- `--debate ... --context-path GLOB` (반복, 최대 20개, 각 1~200자, `**` 지원).
  선택자: `GLOB#Name` / `GLOB#Type.Method`(Go 최상위 func·method·type·var·const, Python def·class·메서드),
  `GLOB:START-END`(줄 범위). 일부만 고른 파일에는 개요(import와 최상위 선언, 줄 번호)가 함께 들어간다.
  같은 파일에 전체 선택과 일부 선택이 겹치면 전체가 우선한다 `--context-ref REF` (기본 `HEAD`, `^[A-Za-z0-9._/~^-]{1,100}$`이고 `-`로 시작하지 않음)
- 팀 파일 선택 필드: `"context": {"paths": [GLOB...], "ref": "HEAD"}`. 요청의 경로가 있으면 요청이 우선한다.
- `--thread`로 이어갈 때 경로를 주지 않으면 그 스레드가 쓴 경로와 커밋을 그대로 쓴다.
- 경로 검증 실패나 ref를 찾지 못하면 exit 2(팀 검증 실패와 같은 코드).
- 결과 JSON에 `"context"`가 추가된다(경로가 없으면 `null`):
  ```json
  {"ref": "HEAD", "sha": "<40자>", "paths": ["apps/agent/*.go"],
   "files": [{"path": "apps/agent/main.go", "bytes": 10240, "truncated": false}],
   "skipped": [{"path": "secrets.enc", "reason": "secret pattern"}],
   "total_bytes": 10240, "tree_files": 87}
  ```
  `skipped.reason`: `secret pattern` · `symlink` · `binary` · `limit`(파일 수·총량 초과) · `symbol not found`(선택자의 glob에 맞는 어느 파일에도 그 심볼이 없음; `path`는 선택자 전체).
  일부만 고른 파일의 `files` 항목에는 `"parts": ["#executeSSH", "lines 110-145"]`와 `"outline": true|false`가 추가된다.
- 액션(action) JSON에 `"base_sha"`(문자열 또는 null)가 추가된다. 파일을 바꾸는 액션는 작업 클론 HEAD가 `base_sha`와 다르면
  실행이 거부된다(exit 4, 409): `"workspace moved from <base> to <head>; debate again on the current code"`.
- `--teams --json`의 각 팀 `data`/`lineup`에 `context`가 포함될 수 있다.

### Go hub API

- `POST /api/debates` 본문에 선택 필드 `"context_paths": [string]`(최대 20, 각 1~200자, NUL·줄바꿈 불가, `-`로 시작 불가),
  `"context_ref": string`(위 패턴). CLI에는 `--context-path=<p>`, `--context-ref=<r>` 형태(한 인자)로 넘긴다.
- 작업 요약에 `context_paths`, `context_ref`를 그대로 담는다(결과의 `context`는 `result`에 있음).

### 대시보드

- 토론 콘솔: "참고할 코드" 텍스트 영역(줄마다 glob 하나)과 ref 입력(비우면 HEAD).
- 팀 편집기: 기본 참고 경로(줄마다 glob)와 ref.
- 결과 화면: "참고한 코드: `<sha 7자리>` · 파일 N개 · X KB" 요약, 펼치면 파일 목록(잘림 표시)과 제외 목록(이유).
- 액션 상세: `base_sha`를 표시하고, 파일을 바꾸는 액션는 "작업 클론이 이 커밋에서 바뀌었으면 실행이 거부된다"고 안내.

## 6. 판정자 출력: 결정 요구사항과 구현 확인 항목 분리

판정자의 JSON 블록은 `{"decision", "constraints", "checks", "actions"}`다.

| 필드 | 뜻 | 쓰이는 곳 |
|---|---|---|
| `decision` | 판정자의 완결된 최종 접근(비판에서 채택한 내용과 이유·트레이드오프 포함, 최대 3000자) | jev 요청의 `chosen`(검토 대상). 없으면 판정문 본문, 그것도 없으면 제안자 원안 |
| `constraints` | 1~8개. 각각 결정의 한 문장을 다시 쓴 것이어야 하며(결정에 없는 세부를 요구사항에만 두지 않음), 서로·액션와 모순되면 안 됨 | jev가 하나씩 검증 |
| `checks` | 액션 실행 후 코드에서 확인할 항목 | jev에 보내지 않음. `final_decision.checks`, DB `decisions.implementation_checks`에 저장, 대시보드에 표시 |
| `actions` | 제안 액션 | 사람 승인 후 실행. jev 요청 `context`에도 요약(도구, 대상, 내용 600자)을 넣어 결정과 어긋나는 액션를 찾게 함 |

이전에는 jev의 `chosen`이 제안자 원안이었다. 판정자가 비판을 채택해 바꾼 요구사항을 원안과 대조하게 되어
2026-10-04의 세 차례 비교 토론이 모두 `DISPUTED`로 끝났다(예: 원안의 "초과 값 잘라 쓰기"와 판정의 "400 거부").

결과 JSON의 `final_decision`에 `decision`(문자열), `checks`(문자열 목록)가 추가된다.

2026-10-04 네 번째 비교 토론에서 jev는 결정에 적힌 요구사항(기본값, 400 거부, 프로세스 그룹 종료)은 통과시켰고,
결정 요약에는 없고 요구사항에만 있던 세부(출력 한도, cgroup, 원격 감독 세부)는 "불확실"로 판정했다.
그래서 판정자에게 결정을 완결되게 쓰고, 요구사항은 결정의 문장을 다시 쓴 것으로 제한하도록 지시한다.

## 7. 부분 수정 도구 `jev_mcp:apply_patch`

설계: [debate-patch-tool.ko.md](debate-patch-tool.ko.md).

- 인자: `{"edits": [{"file_path", "old", "new"}], "check": bool?}`. `old == ""`이면 새 파일.
- 액션 JSON(`--show-action --json`, `GET /api/actions/{id}`)에 `"diff"`(문자열, 기준 커밋 대비 unified diff)가 추가된다.
  `apply_patch`와 `execute_code_action`에만 있고, 다른 도구나 만들 수 없을 때는 없다. 목록(`--actions`)에는 넣지 않는다.
- 실행 결과(`action.result`):
  ```json
  {"status": "SUCCESS|FAILED", "message": "applied 3 edits to 2 files",
   "diff": "<실제 적용된 unified diff>",
   "checks": [{"cmd": "go build ./...", "cwd": "apps/agent", "exit_code": 0, "stdout": "", "stderr": ""}],
   "reverted": false, "error": "<실패 사유, 실패 시>"}
  ```
  검증(`checks`)이 하나라도 실패하면 `status: FAILED`, `reverted: true`이며 파일은 원래대로 돌아간다.
- 판정자 페르소나와 `cross-review` 팀의 허용 도구에 `jev_mcp:apply_patch`가 추가된다.

## 8. 스레드 닫기 (`CLOSED`)

스레드를 삭제하지 않고 `CLOSED` 상태로 둔다. 메시지·결정·jev 검토·액션·에이전트 세션은 그대로 남고, 대시보드의 기본 목록에서만 숨긴다.

- **Python CLI**: `guardian.py --close-thread THREAD [--json]`. `threads.status`를 `CLOSED`로 바꾸고 `threads.closed_at`(기존 DB는 시작 시 `ALTER TABLE`로 추가)에 시각을 남긴다.
  이미 `CLOSED`면 아무것도 바꾸지 않고 `already_closed: true`(멱등). 없는 스레드는 종료 코드 3.
  성공 출력: `{"ok":true,"thread_id","status":"CLOSED","previous_status","already_closed","closed_at"}`. 오류: `{"ok":false,"error":"thread X not found"}`.
- **다시 열기**: `CLOSED` 스레드를 `--debate ... --thread THREAD`로 이어 가는 것은 막지 않는다. 토론이 끝나면 `save_debate_session`이 상태를 평소처럼 다시 쓰고 `closed_at`을 비운다.
  `--jev-recheck`는 jev 검토와 `HELD` 액션는 갱신하지만 `CLOSED` 상태는 바꾸지 않는다.
- **목록**: `DecisionDBManager.list_project_threads(project, include_closed=True)`는 상태를 표시하고, `include_closed=False`면 `CLOSED`를 뺀다.
- **Go hub API**: `POST /api/threads/{thread}/close` → `--close-thread THREAD --json`. `{thread}` 검증은 `jev-recheck`와 같다. `GUARDIAN_UI_ACTIONS=off`이면 403.
  이 스레드를 이어 가는 토론이 이 hub에서 실행 중이면 409. 없는 스레드는 CLI의 종료 코드 3을 따라 404(`error`에 사유).
  대시보드 목록은 토론 작업(`debate-jobs/*/meta.json`) 기반이라, 닫기에 성공하면 같은 `thread_id`의 작업마다 `"thread_closed": true`를 기록하고,
  그 스레드의 토론이 다시 정상 종료되면 지운다. CLI로 직접 닫은 스레드는 이 표시가 없다(DB 상태만 `CLOSED`).
- **대시보드**: `APPROVED`가 아닌 결과에 "Close thread" 버튼(확인 창 포함). "Recent debates"와 개요의 최근 토론은 닫힌 스레드를 기본으로 숨기고,
  토론 화면의 "Show closed" 체크박스로 보여 준다(`CLOSED` 배지). 닫힌 스레드의 결과에는 "Reopen with a new debate"(=스레드 이어 가기)가 나온다. Agents 모니터는 실행 이력이라 숨기지 않는다.
- **닫기는 숨김일 뿐이다 (결정 D10)**: `CLOSED`는 목록에서만 숨긴다. 스레드의 액션(`PROPOSED`/`HELD`/`EXECUTED`)와 게시 기록은 그대로이며, 닫아도 실행·거부·게시가 막히거나 취소되지 않는다. 액션를 정리하려면 닫기 전에 실행하거나 거부한다.
- **닫힌 스레드 목록**: `GET /api/threads/closed` → 200 `{"threads": ["thread_..."]}` (정렬됨). `--closed-threads --json` 결과를 `GET /api/debates`와 같은 30초 캐시에서 읽고(닫기 성공 시 무효화), CLI가 없으면 503, DB를 읽지 못하면 500.

## 9. 게시 기록과 작업 클론 초기화

### 게시 기록 (결정 D13)

- CLI: `guardian.py --publications THREAD --json` → 종료 코드 0 `{"publications": [{"id","branch","commit_sha","remote","status","error","actor","created_at"}]}`, 최신 순. 없는 스레드는 3.
- Go: `GET /api/threads/{thread}/publications` (`h.guard`, 종료 코드 매핑은 위와 같다). 스레드당 게시는 한 번만 성공한다(결정 D11, `publish-thread.md`).
- **게시된 스레드는 이어 갈 수 없다**: `guardian.py --debate T --thread X`는 X에 `PUSHED` 게시가 있으면 종료 코드 4로 `thread X was published as branch B; start a new thread`를 출력하고, `POST /api/debates`(`thread_id` 지정)는 같은 문구로 409를 돌려주며 작업을 만들지 않는다.
  `guardian.py --published-threads --json` → `{"ok":true,"threads":[ids]}`. `GET /api/debates`의 각 항목에는 `thread_closed`와 같은 방식(30초 캐시, 게시 성공 시 무효화)으로 `"thread_published": true`가 붙는다.

### 작업 클론 초기화 (결정 D12)

- CLI 계획: `guardian.py --workspace-reset PROJECT --plan --json` → 0 `{"ok":true,"project_id","workspace","discard":[{"path","state"}],"keep":[{"path","action_id","thread_id"}]}`.
  `state`는 `modified`, `untracked`, `deleted`. `keep`은 `EXECUTED`이고 아직 게시되지 않은(`PUSHED` 기록이 없는 스레드의) 파일 액션가 기록한 `file_hashes`의 경로 중 현재 변경된 것이고, `action_id`는 그 경로를 마지막으로 쓴 액션다. `discard`는 그 밖의 모든 변경·추적 안 되는 경로다.
- CLI 실행: `guardian.py --workspace-reset PROJECT --yes --json [--actor A]` → 0 `{"ok":true,"discarded":[paths]}`. `discard` 목록의 경로만 `git checkout HEAD --`와 `git clean -f --`(스테이지된 새 파일은 먼저 `git rm --cached`)로 되돌리며, 전체 트리 정리나 `git add -A`는 쓰지 않고 `keep` 경로는 건드리지 않는다. 게시·동기화와 같은 작업 클론 잠금을 잡고(사용 중이면 4), 클론이 추적 브랜치가 아니거나 실행 중인 저장소 자신이면 4로 거부한다. `--plan`도 같은 전제 조건을 검사한다. 모르는 프로젝트 3, 잘못된 입력 2.
- **스레드 단위 해제 (`--release`)**: 계획의 `keep` 항목은 `{"path","action_id","thread_id"}`이고 스레드별로 묶어 볼 수 있다. `--workspace-reset PROJECT --yes --release THREAD [--release THREAD2 ...]`(API 본문 `{"confirm": true, "release": ["thread_..."]}`, 최대 20개)는
  그 스레드들이 보관 중이던 파일도 버린다. 해제한 스레드의 `EXECUTED` 파일 액션마다 결과에 `released_by`, `released_at`(UTC)을 기록하고(상태는 그대로) 이후 초기화 계획에서도 `keep`으로 잡지 않으며, 그 스레드의 게시는
  `thread T was released: ... start a new thread` (409)로 거부된다. 해제할 파일 액션가 없거나 이미 게시된 스레드는 4(아무것도 바뀌지 않음). `--plan`과는 함께 쓸 수 없다(2). 응답은 `{"ok":true,"discarded":[paths],"released":[threads]}`.
- **실행 중인 액션와의 조율**: 프로젝트에 `RUNNING` 액션가 있으면 계획과 실행 모두 4/409 `actions are running in this project: #12, #15`로 거부한다(멈춘 액션는 `--fail-stale-action`으로 정리). 파일 액션(`execute_code_action`, `apply_patch`)는 실행하는 동안(파일 쓰기와 `run_test`) 게시·동기화·초기화와 같은 작업 클론 잠금을 잡으므로, 서로 끼어들지 못하고 잠금이 사용 중이면 액션는 `RUNNING`으로 바뀌기 전에 4로 거부된다.
- Go: `GET /api/projects/{id}/workspace-reset`(계획, 읽기 전용이라 `GUARDIAN_UI_ACTIONS=off`여도 허용)과 `POST /api/projects/{id}/workspace-reset`(본문 `{"confirm": true}` 필수, 아니면 400. `GUARDIAN_UI_ACTIONS=off`이면 403). 둘 다 `h.guard`, 종료 코드는 `respondConfig`로 매핑한다.

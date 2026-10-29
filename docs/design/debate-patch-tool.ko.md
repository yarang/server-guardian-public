# 판정자 액션: 부분 수정 도구 (설계)

[English](debate-patch-tool.md) | 한국어

## 문제

판정자의 코드 수정 도구 `jev_mcp:execute_code_action`은 **파일 전체를 새로 쓴다**. 코드 묶음에서 함수·줄 범위만 본 판정자는
파일 전체 내용을 모르므로 기존 파일을 안전하게 고칠 수 없다. 2026-10-04 다섯 번째 비교 토론(`thread_20261004_065316_c36e2b`)은
처음으로 jev를 통과했지만, 액션는 새 상수 파일 하나뿐이었고 핵심 변경(`executeRaw`·`executeSSH` 수정, 처리기의 400 검증,
`CommandResponse` 필드 추가)은 "파일 전체가 없어 안전한 재작성이 불가"하다며 제안하지 않았다.

## 형식 선택

| 형식 | 예 | 장점 | 단점 |
|---|---|---|---|
| unified diff | `@@ -248,7 +248,9 @@` | 사람에게 익숙, git 도구로 적용 | LLM이 줄 번호·문맥 줄 수를 자주 틀림. 부분 코드만 본 경우 문맥 줄이 맞지 않기 쉬움 |
| **찾아 바꾸기(권장)** | `{"file_path", "old": "정확한 원문", "new": "바꿀 내용"}` | 줄 번호가 필요 없음. 원문이 정확히 한 번 나와야 적용되므로 잘못 적용될 수 없음. Claude Code의 Edit 도구와 같은 방식 | 원문이 길면 프롬프트·액션가 커짐 |

## 도구 정의 (권장안)

`jev_mcp:apply_patch` (위험도 medium)

```json
{
  "edits": [
    {"file_path": "apps/agent/metrics.go",
     "old": "func executeRaw(command string, timeoutSec int) (int, string, string, int64) {\n\tif timeoutSec <= 0 {\n\t\ttimeoutSec = 10\n\t}",
     "new": "func executeRaw(ctx context.Context, command string, timeoutSec int) (int, string, string, int64, bool) {\n\tif timeoutSec <= 0 {\n\t\ttimeoutSec = ExecDefaultTimeout\n\t}"},
    {"file_path": "apps/agent/timeout_policy.go", "old": "", "new": "package main\n..."}
  ],
  "check": true
}
```

- `old`가 빈 문자열이면 새 파일 만들기. 그 파일이 이미 있으면 거부한다.
- 같은 파일에 여러 편집을 둘 수 있고 순서대로 적용한다. 각 `old`는 **그 시점의 파일 내용에서 정확히 한 번** 나와야 한다.
- 파일 삭제와 이름 변경은 지원하지 않는다.
- 한도: 파일 10개, 편집 30개, `old`·`new` 각 20KB, 전체 100KB.

## 안전장치

| 시점 | 확인 | 실패 시 |
|---|---|---|
| 제안 저장 시(`record_proposals`) | 경로가 작업 클론 안이고 `.git`·비밀 패턴·심볼릭 링크가 아님. 각 `old`가 **기준 커밋의 파일**에서 정확히 한 번 나옴 | `INVALID` |
| 제안 저장 시 | 각 `old`가 **판정자가 실제로 본 코드 안에** 있음(코드 묶음의 전체 파일 또는 선택된 범위). 보지 않은 코드는 고칠 수 없다 | `INVALID` |
| 실행 직전 | 작업 클론 HEAD가 기준 커밋과 같음(기존 규칙). 대상 파일에 커밋하지 않은 변경이 없음(같은 결정의 형제 액션가 만든 변경은 예외, 아래 "형제 액션") | 409 |
| 실행 | 모든 파일의 새 내용을 메모리에서 먼저 만들고, 하나라도 실패하면 아무것도 쓰지 않음(전부 적용 또는 전부 미적용) | `FAILED` |
| 실행 후 | 결과에 `git diff` 전문을 저장해 무엇이 바뀌었는지 남김 | - |

함께 바꾸는 것:
- `execute_code_action`으로 **이미 있는 파일**을 덮어쓰는 것은, 그 파일 전체가 코드 묶음에 있었을 때만 허용한다. 부분만 본 파일을 통째로 다시 쓰는 일을 막기 위해서다. 새 파일은 지금처럼 허용한다.
- 판정자 페르소나(`jev_mcp_decision`)의 `mcp_capabilities`와 `cross-review` 팀의 `allowed_tools`에 `apply_patch`를 추가한다.
- 판정자 프롬프트: 기존 파일은 `apply_patch`로, 새 파일만 `execute_code_action`으로 고치라고 안내한다.

## 승인 화면

- 대시보드와 `--show-action`에 **기준 커밋 대비 unified diff**를 보여 준다. 기준 커밋의 파일에 편집을 적용해 Python `difflib`로 만든다.
  사람은 찾아 바꾸기 JSON이 아니라 익숙한 diff를 보고 승인한다.
- 실행 후 화면에는 실제 `git diff`와 구현 확인 항목(`checks`)을 함께 보여 준다.
- jev 요청의 액션 요약에도 파일별 변경 줄 수와 diff 앞부분을 넣는다.

(2026-10-18: 2026-10-16부터 `apply_patch`와 `run_test`가 없는 `execute_code_action`은 jev 게이트가 PASSED이면 승인 없이 자동 적용된다(`decided_by` `auto:engine`). 승인 화면에서는 실행 결과로 보이며, 작업 클론의 변경을 저장소로 올리는 게시(publish)가 사람의 마지막 관문이다. `docs/design/debate-agent-retrieval.md`의 "구현 메모 (코드 변경 자동 승인)"와 `docs/design/publish-thread.md` 참고.)

## 실행 후 검증 (`check: true`)

허브에는 python3와 node만 있고 Go와 pytest는 없었다(2026-10-04 확인). 이후 설치 스크립트가 Go를 설치한다(아래 "허브 Go"). pytest는 여전히 없다.

| 대상 | 검증 |
|---|---|
| `.py` | `python3 -m py_compile <파일>` |
| `.go` | 허브에 Go 도구가 없어 지금은 검증 불가. 선택지: 허브에 Go 설치(arm64 약 70MB) 후 `gofmt -l`과 `go build ./...`, 또는 검증 없이 diff 확인 후 `git_commit_push`로 브랜치에 커밋해 CI/로컬에서 검증 |
| 그 밖 | 없음 |

검증이 실패하면 액션는 `FAILED`가 되고 바뀐 파일은 원래대로 되돌린다(`git checkout -- <파일>`, 작업 클론이므로 안전).

## 작업 범위

| 계층 | 변경 |
|---|---|
| Python | `mcp_client`에 도구 정의·검증·실행(찾아 바꾸기, 원자적 적용, 되돌리기), 코드 묶음에서 "본 범위" 계산, `execute_code_action` 덮어쓰기 제한, diff 생성, 판정자 프롬프트, 페르소나·팀 설정 |
| Go | 변경 없음(액션 API는 도구 이름과 무관) |
| 대시보드 | 액션 상세에 diff 표시(추가 줄 초록, 삭제 줄 빨강), 실행 결과의 `git diff` 표시 |

## 결정 (2026-10-04)

1. 형식: 찾아 바꾸기
2. 판정자가 본 코드만 수정 가능: 적용. 코드 묶음이 없는 토론의 액션는 새 파일만 만들 수 있다
   (2026-10-18: 탐색(`debate-agent-retrieval.md`)이 생긴 뒤에는 판정자가 repo 블록이나 MCP 도구로 읽은 코드도 본 코드로 센다.)
3. Go 검증: 허브에 Go 설치(설치 스크립트가 `~/.local/go`에 공식 배포본을 sha256 확인 후 설치). 적용 직후 `gofmt -l`과
   변경된 파일이 속한 모듈의 `go build ./...`를 실행하고, 실패하면 되돌린다

## 구현 메모 (2026-10-04)

- 코드: `cli/llm-agent/patch_ops.py`(검증·적용·되돌리기·diff), `mcp_client`의 도구 정의, `action_ops.proposal_checks`(제안 시 검사),
  `code_context.ContextPack.visible/whole`(판정자가 본 코드).
- 허브 Go: 설치 스크립트가 go.dev의 공식 배포본을 받아 go.dev가 공개한 sha256과 대조한 뒤 `~/.local/go`에 설치한다(`HUB_GO_VERSION`, 기본 1.26.8). (2026-10-18: 기본값은 이제 `apps/agent/go.mod`의 `toolchain` 줄(없으면 `go` 줄)에서 읽는다. 지금은 go1.26.8이다.)
  `GOTOOLCHAIN=local`, `GOFLAGS=-mod=readonly`로 실행해 검증 중 네트워크로 다른 툴체인이나 모듈을 받지 않는다.
- gofmt: 이 저장소의 `metrics.go`, `model.go`, `fleet_handler.go`는 원래 gofmt 형식이 아니다. 편집 전부터 형식이 아니던 파일은
  gofmt 검사에서 실패로 보지 않고 보고만 한다. 편집이 새로 형식을 깨뜨린 경우에만 실패하고 되돌린다.
- 코드 묶음이 없는 토론의 액션는 새 파일만 만들 수 있다(보지 않은 기존 코드는 수정 불가).
- import 블록(2026-10-04 여섯 번째 토론 후 보완): 부분 선택한 Go·Python 파일의 import는 개요가 아니라 원문 그대로 넣고
  수정 가능한 범위에 포함한다. 여섯 번째 토론에서 판정자가 `context`·`syscall` import를 추가하는 편집을 냈다가 "본 적 없는 코드"로
  `INVALID`가 됐다. 같은 6개 편집(4개 파일)을 새 규칙으로 기준 커밋 `89fb0fd`에 재현하면 검사를 통과하고 `go build`, `go vet`,
  기존 `go test`가 모두 성공한다.
- `run_tests`의 Go 명령은 패키지 경로가 속한 모듈 디렉터리에서 실행한다(`go test ./apps/agent/...` → `apps/agent`에서 `go test ./...`).
  실행 PATH 앞에 `~/.local/go/bin`과 `~/.local/bin`을 붙이고 `GOTOOLCHAIN=local`로 실행한다(허브 에이전트의 PATH에는 둘 다 없다).
- 기준점 줄(여덟 번째 토론 후 보완): 판정자가 새 상수를 넣으려고 개요에만 나온 `const AgentVersion = "..."` 한 줄을 기준점으로 썼다가
  `INVALID`가 됐다. 개요 중 원문 한 줄 그대로인 Go 선언(`package`, `const`, `var`, `type`)은 본 코드로 인정한다. 함수 시그니처 줄은
  본문을 보지 않고 시그니처를 바꾸는 것을 막기 위해 계속 제외한다.
- `run_tests`의 `go test ./...`는 루트에 `go.mod`가 없고 저장소의 Go 모듈이 하나뿐이면 그 모듈에서 실행한다. 여럿이면 제안 시점에 거부한다.
- 액션 순서: 여덟 번째 토론의 액션는 #8(상수·처리기·`executeRaw`) → #9(`executeSSH`, #8의 상수 사용) → #10(`go test`) 순서여야 한다.
  기준 커밋 `5980d68`에 새 규칙으로 재현하면 이 순서로 셋 다 성공하고(`go build`, `go test` 통과), #9만 먼저 실행하면 `go build` 실패로
  자동으로 되돌려진다. 순서를 강제하는 장치는 아직 없고 빌드 검사가 잘못된 순서를 막는다.

## 형제 액션 (2026-10-12)

문제: 한 판정이 같은 기준 커밋에 맞춰 같은 파일을 고치는 액션 둘(#45, #47)을 냈다. #45가 실행되어 파일이 수정(미커밋) 상태가 되자
#47이 "uncommitted changes in the files to edit"로 실패했다. 액션는 사람이 대시보드에서 하나씩 승인해 실행한다.

규칙: 대상 파일의 미커밋 변경은 **그 변경을 모두 같은 스레드·같은 `base_sha`의 EXECUTED 액션(형제)가 만든 경우에만** 허용한다.
- 형제가 실행되면 결과(`result`)에 `file_hashes`(파일 경로 -> 적용 직후 내용의 sha256)와 `applied_at_ns`를 기록한다. 새 컬럼은 없다.
  `apply_patch`와 `execute_code_action` 모두 성공 시 기록한다.
- 다음 액션는 실행 직전에 `action_ops.sibling_hashes`로 같은 스레드·같은 `base_sha`의 EXECUTED 파일 액션에서 파일별
  마지막(`applied_at_ns`, 다음 `id`) 해시를 모은다. 파일이 미커밋 상태이면 현재 내용의 해시가 그 값과 같아야 한다.
  사이에 사람이 고쳤거나(해시 불일치), 다른 스레드·다른 `base_sha`·FAILED 액션가 남긴 변경이면 거부한다.
- HEAD가 `base_sha`와 같아야 한다는 기존 규칙은 그대로다. 파일이 깨끗하면 형제 기록은 필요 없다.
- `execute_code_action`도 같은 검사를 받는다(이전에는 미커밋 검사가 없었다). 이미 있는 파일을 통째로 덮어쓰는 제한은 그대로다.
- 편집은 형제 변경이 들어 있는 작업 트리 내용에 대해 맞춘다. `old`는 여전히 정확히 한 번 나와야 하며, 형제가 그 텍스트를 바꿨다면
  평소처럼 "old text not found"로 실패한다.
- `check` 실패 시 되돌리기는 이 액션 직전의 내용(형제가 적용한 상태)으로 복원한다. HEAD로 돌리지 않는다.
- 거부 메시지는 파일 이름과 "the change was not made by a sibling action of this decision"을 밝힌다.
- `run_tests`는 그대로이다. `git_commit_push`는 폐지되었다(게시를 쓴다).

구현: `patch_ops.uncommitted_conflicts/file_hashes`, `mcp_client.call_tool(..., siblings=)`, `action_ops.sibling_hashes/run_action`.
(2026-10-18: 코드 변경 자동 적용(`action_ops.auto_run_actions`)은 같은 스레드의 액션를 id 오름차순으로 실행해 이 연쇄를 그대로 쓰고, `EXECUTED`가 아닌 액션에서 멈춘다.)
테스트: `test_patch_ops.SiblingActionTests`.

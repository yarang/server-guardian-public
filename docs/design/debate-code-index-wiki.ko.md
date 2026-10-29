# 토론 에이전트의 심볼 인덱스, 저장소 지도, 코드 위키 (설계)

[English](debate-code-index-wiki.md) | 한국어

`debate-agent-retrieval.md`의 탐색(repo 블록, `repo_*` MCP)을 바꾸지 않고 그 위에 길잡이를 더한다.
탐색은 여전히 근거를 얻는 유일한 길이고, 이 문서의 모든 기능은 "어디를 읽을지"를 줄여 주는 역할만 한다.

지금 값(2026-10-18): `INDEX_VERSION` 2(`code_index.py`), `MAP_VERSION` 3(`repo_map.py`), `GOINDEX_VERSION` 1(`go_facts.py`),
`WIKI_PROMPT_VERSION` `2026-10-16.wiki6`(`wiki_build.py`), `RETRIEVAL_PROMPT_VERSION` `2026-10-17.p10`(`debate_engine.py`. p2.6 뒤의
올림은 이 문서 밖의 기능에서 왔다). 아래 구현 메모에 적힌 버전 값은 그 단계 당시의 값이다.

## 문제

1. **언어 편중.** `outline`과 `path#Symbol`은 Go·Python만 지원한다(`code_select.language`). 2026-10-12 기준 추적 파일 479개, 4.2MB 중
   TSX 843KB·TS 249KB(apps/web-desktop 등)는 개요가 없어 grep과 전체 읽기에 기댄다.
2. **관계 탐색 부재.** "이 함수를 누가 부르나", "이 타입은 어디서 정의됐나"를 물을 op가 없어 에이전트가 정규식 grep으로 흉내 낸다.
3. **과다 읽기.** 2026-10-09 허브 실행에서 Expert 한 단계가 1,301개 항목, 5.6MB를 요청했다. 길을 찾을 수단이 없으면 많이 읽는 쪽으로 버틴다.
4. **첫 진입점.** 첫 프롬프트는 파일 목록(최대 500줄)만 준다. 경로 이름 외에는 무엇이 어디 있는지 알 수 없다.
5. **설계 의도.** "왜 이렇게 되어 있나"는 코드만 읽어서는 매번 다시 추론해야 하고, 토론마다 같은 탐색을 반복한다.

## 결정 요약

| 층 | 내용 | 정확도 | 근거로 쓸 수 있나 |
|---|---|---|---|
| 0. 탐색 (기존) | `list`/`grep`/`read`/`outline` | 원문 | 예 |
| 1. 심볼 인덱스 | 고정 sha의 선언·참조 표. `defs`/`refs` op, TS/TSX `outline` | 기계 생성, sha와 일치 | 위치 정보로만(읽은 것으로 세는 범위는 아래 "인용 검증") |
| 2a. 저장소 지도 (위키 생성 영역) | 인덱스에서 sha마다 만드는 디렉터리·파일·주요 심볼 요약. 첫 프롬프트와 위키 페이지 머리에 들어감 | 기계 생성 | 아니오 |
| 2b. 위키 서술 영역 | 디렉터리·기능 단위 설명(역할, 흐름, 주의점). 근거 파일과 blob 해시를 기록하고 바뀌면 `stale` | LLM 작성, 낡을 수 있음 | **아니오** |

- 2a와 2b는 한 위키 페이지의 두 영역이다. 페이지를 열면 2a(현재 sha 기준으로 다시 생성)와 2b(저장된 설명과 낡음 표시)를 함께 보여 준다.
- 사람·LLM은 2a를 고치지 않는다. 2b는 위치를 문장으로 적지 않고 인용(`path:N`, `path#Symbol`)으로만 적는다. 인용은 읽을 때 현재 sha로 검사한다.
- 의존성은 표준 라이브러리만 쓴다(`requirements.txt`의 원칙). tree-sitter·LSP·임베딩은 쓰지 않는다.

## 1층. 심볼 인덱스 `cli/llm-agent/code_index.py`

### 파서

`code_select`와 같은 원칙(gofmt·prettier로 정리된 코드를 가정한 레이아웃 파싱)을 쓰고, 언어별 함수 하나가 한 파일의 선언과 식별자 출현을 돌려준다.

| 언어 | 선언 | 참조 |
|---|---|---|
| Python | `ast`: 모듈 수준 `def`/`class`, 클래스 안 메서드(`Type.method`), 모듈 수준 대입 상수 | `ast`의 `Name`·`Attribute`·`Call` 노드 식별자와 줄 |
| Go | 기존 `_go_decl_span` 규칙: 0열 `func`/`func (r T)`/`type`/`var`/`const` 블록 | 주석·문자열을 뺀 식별자 토큰 |
| TS/TSX/JS | 0열 `export`? + `function`/`class`/`interface`/`type`/`enum`/`const X =`/`let X =`, `export default function X`. 끝 줄은 다음 0열 `}`·`};` 또는 다음 0열 선언 앞 | 주석·문자열·템플릿 리터럴을 뺀 식별자 토큰, JSX 태그 이름(`<Name`) 포함 |
| 그 밖 | 없음(경로만 지도에 나온다) | 없음 |

- 선언 항목: `{"name", "qualified", "kind", "path", "start", "end", "signature"}`. `signature`는 선언 첫 줄을 200자로 자른다.
- 참조는 **이름 기반**이다. 타입 정보가 없으므로 같은 이름의 다른 심볼도 섞인다. 결과에 이 사실을 적는다(아래 `refs`).
- 파싱 실패(문법 오류, 비정형 레이아웃)는 그 파일을 "선언 없음"으로 두고 인덱스 전체를 실패시키지 않는다. `index_errors`에 경로만 남긴다.
- `outline`(기존 op)은 이 파서를 쓰도록 바꾸고 TS/TSX/JS까지 지원한다. `path#Symbol`도 같은 선언 표로 찾는다(Go·Python 결과는 지금과 같아야 하며 기존 `test_code_select.py`가 그대로 통과해야 한다).

### 범위와 제외

`RepoTools.readable_paths()`와 같은 집합만 색인한다(비밀 패턴, 심볼릭 링크, 서브모듈, NUL 바이트 파일 제외). 파일 하나가 1MB를 넘으면 색인하지 않는다(생성물로 본다).

### 캐시

- 위치: `<GUARDIAN_DATA_DIR>/code-index/<project>/blobs/<blob id 앞 2자>/<blob id>.json`. 파일 내용이 같으면 blob id가 같으므로 sha가 바뀌어도 바뀐 파일만 다시 파싱한다.
- blob id는 `git ls-tree -r -l`의 세 번째 필드다. `RepoTools.entries`에 `(mode, size)`와 함께 blob id를 저장하도록 넓힌다.
- 파서 버전(`INDEX_VERSION`)이 바뀌면 캐시 파일을 무시하고 다시 만든다. 캐시 JSON에는 선언 표(선언 첫 줄은 문자열 리터럴 내용을 `"…"`로 가린 `signature`, P2.1)와 식별자 출현(`{식별자: [줄]}`)만 있고 소스 본문은 없다.
- sha 단위 집계(전체 선언 표, 역참조 표, 지도 순위)는 메모리에서 만들고 `<project>/sha/<sha>.json`에 남긴다. 최근 20개 sha만 유지한다. (2026-10-18: sha 단위 파일은 만들지 않았다. 집계는 메모리에만 있다. 아래 "구현 메모 (P1)", "(P2)".)
- 캐시가 없거나 쓰기 실패해도 동작한다(그 실행에서만 메모리로 만든다).
- 수명(P2.1): 캐시 적중과 쓰기가 파일 mtime을 갱신한다. 프로세스당 처음 캐시를 쓸 때 프로젝트 캐시에서 `GUARDIAN_CODE_INDEX_MAX_AGE_DAYS`(기본 30)보다 오래된 파일을 지우고, 합계가 `GUARDIAN_CODE_INDEX_MAX_MB`(기본 256)를 넘으면 오래된 것부터 지운다(0이면 해당 규칙 끔). 야간 백업 타르볼은 `code-index/`를 뺀다.

### 새 op

| op | 인자 | 결과 |
|---|---|---|
| `defs` | `symbol`(`Name` 또는 `Type.Method`, `SYMBOL_PATTERN`), `glob`(선택) | `path:start-end kind signature` 줄, 최대 100줄 |
| `refs` | `symbol`, `glob`(선택), `include_defs`(기본 false) | `path:line: text` 줄(grep과 같은 형식), 최대 200줄, 줄당 300자. 머리에 "이름 기반 결과이며 같은 이름의 다른 심볼이 섞일 수 있다" 한 줄 |
| `map` | `path`(디렉터리, 기본 루트), `depth`(1~4, 기본 2) | 2a 저장소 지도의 해당 부분 |
| `wiki` | `page`(페이지 id) 또는 `query`(문자열) | 2층 위키(아래) |

- 결과 모양은 기존과 같다(`{"op", "target", "ok", "bytes", "truncated", "error", "text"}`). 모든 예산·`deferred` 규칙은 기존 op와 같이 적용된다.
- `RepoTools.run`의 분기에 추가하고 `repo_mcp.py`에 `repo_defs`, `repo_refs`, `repo_map`, `repo_wiki` 도구로 노출한다. 서버 로그 형식은 같다.
- 프롬프트의 op 설명(`_retrieval_intro`, MCP 안내)에 네 op를 더한다. "위치를 찾을 때는 grep보다 defs/refs를, 큰 파일은 outline을 먼저"라고 안내한다.

### 인용 검증과의 관계

`provided_paths`(에이전트에게 코드가 제공된 파일)에 대한 규칙:

| op | 제공된 것으로 세는 파일 |
|---|---|
| `defs` | 없음. 시그니처 한 줄은 코드 근거가 아니다(`outline`은 지금처럼 센다) |
| `refs` | grep과 같이 줄이 나온 파일 |
| `map` | 없음(`list`와 같다) |
| `wiki` | 없음 |

`mentioned_in`도 같다. `defs`·`map`·`wiki` 텍스트에 나온 경로는 단순 언급 근거로 세지 않는다. 그래서 위키나 지도만 보고 `path:N`을 인용하면 지금처럼 인용 검증에 걸려 되묻는다.

### 구현 메모 (P1)

설계와 다르게 구현했거나 설계에 없던 결정이다.

- sha 단위 JSON(`<project>/sha/<sha>.json`)은 P1에서 만들지 않는다. 집계(선언 표, 식별자 역참조 표)는 `RepoTools` 인스턴스마다 처음 `defs`/`refs`가 올 때 메모리에서 한 번 만들고(락으로 보호), blob 캐시가 있으면 바뀐 파일만 다시 파싱한다. 20개 sha 보관 규칙은 sha 파일이 생길 때(P2 이후) 함께 넣는다.
- 캐시 위치는 `RepoTools(workspace, sha, cache_dir=...)`로 주입한다. 엔진은 `code_index.default_cache_dir(project.id)`를 넘기고, MCP 서버는 `--cache-dir`로 받는다(`McpTurn`이 만든 spec의 `cache_dir`). 인자가 없으면 메모리만 쓴다. `default_cache_dir`는 `GUARDIAN_DATA_DIR`만 읽고 디렉터리를 만들지 않는다(처음 캐시 파일을 쓸 때 생긴다).
- 선언의 `start`는 선언 키워드가 있는 줄이다(앞의 주석 블록은 넣지 않는다). `refs`는 `start` 줄을 정의 줄로 보고 뺀다. 기존 Go `path#Symbol`은 주석 블록을 포함하는 지금 규칙 그대로다.
- 색인 대상은 `readable_paths()` 중 파서가 있는 언어(`.go` `.py` `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs`)뿐이다. 그 밖의 파일은 `defs`/`refs`에 나오지 않는다(grep을 쓴다).
- Python 상수는 모듈 수준의 단순 이름 대입이며 대문자 이름은 `const`, 나머지는 `variable`이다. 중첩 클래스의 메서드는 `Outer.Inner.deep`으로 기록하고 `Inner.deep`으로도 찾는다. Go는 `const (`/`var (`/`type (` 블록 안의 한 탭 들여쓴 이름도 선언으로 기록한다.
- TS/JS 끝 줄 규칙: 선언 줄 다음부터 0열이 `}` `)` `]`인 줄을 만나면 거기서 끝난다. 단 그 줄이 `{` `(` `[` `=>`로 끝나면(`): Foo {`, `} else {`) 시그니처 또는 본문의 연속으로 보고 계속한다. 그 전에 0열의 다른 줄(다음 선언, import/export, 주석)이 나오면 그 앞의 마지막 비어 있지 않은 줄에서 끝난다. 클래스 멤버, 함수 안 선언, 따로 한 줄을 차지하는 데코레이터, CommonJS export는 보지 않는다.
- 식별자 스캔의 한계: 따옴표나 `//`가 들어 있는 정규식 리터럴, JSX 본문의 아포스트로피는 그 줄의 나머지를 가릴 수 있다. 템플릿 리터럴은 글자 부분만 버리고 `${...}` 식은 스캔한다.
- `code_select.language`가 TS 계열에 `"ts"`를 돌려주고 `outline`/`symbol_spans`/`import_spans`가 `code_index`를 늦게 import해 쓴다. 그래서 코드 컨텍스트의 `path#Symbol` 선택도 TS 파일에서 동작한다. Go·Python 결과는 바뀌지 않는다.
- `provided_paths`의 `refs`는 `grep`과 같은 정규식을 쓴다. 머리 줄에는 `path:N: ` 모양이 없어 파일을 제공한 것으로 세지 않는다.

## 1층 확장. 컴파일러 보조 인덱스 (P2.6, Go부터)

### 왜

1층의 `refs`는 이름 기반이다. 같은 이름의 다른 심볼이 섞이고(`run`, `Close`, `ServeHTTP`), 메서드 호출과 패키지 경계를 구별하지 못한다.
2026-10-12 구조 검토(`docs/analysis/2026-10-12-retrieval-architecture-review.md`, W1)는 처음에 표준 라이브러리로 만드는 import 그래프를 P2.6에 두었다.
그런데 컴파일러는 이 관계를 이미 정확히 안다. 그래서 P2.6을 **허브의 툴체인으로 타입 검사한 사실을 뽑아 인덱스에 더하는 방식**으로 바꾼다. 소스는 git에 그대로 두고, 빌드에 쓴 작업 트리와 산출물은 매번 지운다.

- 첫 언어는 Go다. `go list`와 `go/types` 타입 검사는 대상 코드를 실행하지 않는다(`go generate`, 테스트, cgo를 쓰지 않는 조건에서). 허브에는 Go 1.26.8(`~/.local/go/bin`)이 있고, server-guardian의 `apps/agent`는 외부 의존성이 없는 모듈이다.
- TS(`tsc`)는 `node_modules`가 필요하다. `npm install`은 의존성의 설치 스크립트로 임의 코드를 실행할 수 있으므로 이번 단계에 넣지 않는다(아래 "다음 언어").
- Python은 지금처럼 `ast`(1층)를 쓴다.
- 토론 엔진(Python)은 계속 표준 라이브러리만 쓴다. 추출은 별도 실행 파일이 하고, 엔진은 그 결과 JSON만 읽는다.

### 구성

```
고정 sha ──git worktree add --detach──▶ 임시 작업 트리(0700)
                                           │ goindex (Go 도우미, 표준 라이브러리만)
                                           │   go list -json -export -deps ./...
                                           │   go/parser + go/types (gc export data)
                                           ▼
                                  사실 JSON ──▶ <GUARDIAN_DATA_DIR>/code-index/<project>/go/<sha>.json
                                           │
                           git worktree remove --force, 빌드 산출물 삭제
```

1. **임시 작업 트리.** 엔진이 `git -C <workspace> worktree add --detach <tmp>/src <sha>`로 만든다. `<tmp>`는 `mkdtemp`(0700)이다.
   - publish가 쓰는 작업 클론의 작업 트리와 인덱스는 건드리지 않는다. 바뀌는 것은 `.git/worktrees/` 항목뿐이고 끝나면 지운다.
   - 엔진이 시작할 때와 추출 전마다 `git worktree prune`을 한 번 돌려, 이전에 중단된 추출의 항목을 정리한다.
2. **도우미 `goindex`.** 위치는 `tools/goindex/`이고, 별도 `go.mod`를 두며 표준 라이브러리만 쓴다. 설치 스크립트가 agent처럼 교차 빌드해 허브의 `~/.local/libexec/guardian/goindex`에 둔다.
   - 입력: 작업 트리 경로, 모듈 디렉터리 목록(`go.mod`가 있는 디렉터리. `MCPClient._go_modules`와 같은 규칙으로 `.git`·`node_modules`·`vendor` 제외), 출력 파일 경로.
   - 모듈마다 `go list -json -export -deps ./...`로 패키지, 파일, import, export data 경로를 얻는다. 표준 라이브러리 패키지는 사실을 뽑지 않고 export data로만 쓴다.
   - 대상 모듈의 패키지마다 `go/parser`로 파일을 읽고, `go/types`에 `importer.ForCompiler(fset, "gc", lookup)`을 넣어 타입 검사한다. lookup은 `go list`가 준 export 파일을 연다.
   - `types.Info.Defs`·`Uses`·`Selections`로 정의와 참조를 해석한다. 참조마다 그 줄을 감싸는 함수(`FuncDecl`, 메서드면 `Type.Method`)를 기록한다.
   - 테스트 파일(`_test.go`)도 포함한다. `go list -test`의 테스트 패키지를 쓰며, 결과에는 `test: true`로 표시한다.
3. **환경.** 도우미와 그 자식 프로세스에 다음을 강제한다.
   - `CGO_ENABLED=0`: C 컴파일러를 부르지 않는다.
   - `GOTOOLCHAIN=local`: `go.mod`의 `toolchain` 지시문이 있어도 툴체인을 내려받지 않는다.
   - `GOFLAGS=-mod=readonly`(다른 값은 덮어쓴다), `GOPROXY=off`. 모듈 캐시에 없는 의존성은 받지 않고 실패한다(아래 "실패").
   - `GOCACHE=<GUARDIAN_DATA_DIR>/code-index/.gocache`: 사용자 빌드 캐시(`~/.cache/go-build`)와 분리한다.
   - 비밀 환경 변수(토큰, `llm.env` 내용)는 넘기지 않는다(`MCPClient.tool_environ`과 같은 `tool_env` 규칙).
   - `PATH`에 `~/.local/go/bin`을 앞에 둔다. 허브 서비스의 PATH에는 사용자 Go가 없다.
   - 제한 시간은 `GUARDIAN_GOINDEX_TIMEOUT`(기본 180초)이다. 넘기면 프로세스 그룹을 죽이고 실패로 처리한다.
4. **정리.** 성공하든 실패하든 `finally`에서 다음을 한다.
   - `git worktree remove --force <tmp>/src`, 그다음 `<tmp>` 삭제
   - `GOCACHE`가 `GUARDIAN_GOINDEX_CACHE_MB`(기본 1024)를 넘으면 `go clean -cache`
   - 바이너리를 만들지 않는다(`go build`를 부르지 않고 `go list -export`만 쓴다). 작업 트리 밖에 남는 산출물은 `GOCACHE`뿐이다.

### 사실 형식

`<GUARDIAN_DATA_DIR>/code-index/<project>/go/<sha>.json`이다. 키는 `(sha, GOINDEX_VERSION, go version)`이고, 셋 중 하나라도 다르면 다시 만든다.

```json
{"version": 1, "sha": "…", "go": "go1.26.8", "modules": ["apps/agent"],
 "packages": [{"path": "github.com/server-guardian/agent", "dir": "apps/agent", "imports": ["net/http", "…"],
               "errors": []}],
 "defs": [{"id": 17, "name": "followEvents", "qualified": "debateHub.followEvents", "kind": "method",
           "pkg": "github.com/server-guardian/agent", "path": "apps/agent/debate_events.go", "start": 168, "end": 230,
           "exported": false, "test": false}],
 "refs": [{"def": 17, "path": "apps/agent/debate_events.go", "line": 120, "enclosing": "debateHub.debateEvents",
           "call": true, "test": false}],
 "unresolved": [{"path": "apps/agent/x.go", "line": 9, "name": "Foo", "reason": "type error"}]}
```

- 경로는 저장소 기준 상대 경로다. `readable_paths()`에 없는 파일(비밀 패턴, 심볼릭 링크)의 정의·참조는 **쓰지 않는다**. 컴파일에는 그 파일도 쓰이지만 사실 JSON에는 남지 않는다.
- 소스 본문, 문자열 리터럴, 주석은 넣지 않는다(위치와 이름만). 그래서 이 파일은 1층 캐시보다 담는 내용이 적다.
- `call`은 참조가 호출 식(`CallExpr`의 함수 부분)인지 여부다. 인터페이스 메서드 호출은 인터페이스 메서드 정의를 가리키며, 어떤 구현이 불리는지는 풀지 않는다(아래 "한계").
- sha 파일은 프로젝트마다 최근 20개만 남긴다. 결정에 따라 백업에서 빼고, 오래된 것부터 지운다.

### 언제 만드나

- 토론의 탐색이 켜질 때(`_setup_retrieval`) 그 sha의 사실 파일이 없고 저장소에 `go.mod`가 있으면, **백그라운드 스레드**에서 추출을 시작한다. 토론 시작을 기다리게 하지 않는다.
- `callers`·`refs`·`defs`가 Go 대상으로 처음 들어올 때 추출이 진행 중이면 최대 `GUARDIAN_GOINDEX_WAIT`(기본 45초. 일부 CLI(codex)의 MCP 도구 호출 제한 60초보다 짧게)만 기다린다. 그 안에 끝나지 않으면 1층의 이름 기반 결과를 주고, 결과 머리에 "타입 정보 준비 중, 이름 기반 결과"라고 적는다.
- 같은 sha의 추출은 프로젝트당 하나만 돈다(파일 잠금 `<sha>.lock`). 동시에 시작한 두 토론은 같은 결과를 기다린다.
- 프로젝트 동기화(`--project-sync`) 직후에도 새 HEAD에 대해 미리 추출할 수 있다(선택, `GUARDIAN_GOINDEX_PREFETCH=1`). (2026-10-18: 만들지 않았다. 아래 "구현 메모 (P2.6)".)

### op 변화

| op | 변화 |
|---|---|
| `refs` | Go 대상(심볼의 정의가 Go 파일이거나 `glob`이 Go만 고를 때)이고 사실이 있으면, 타입 검사로 해석한 참조만 준다. 머리는 `[refs of X: type-checked (go1.26.8), N packages; non-Go files are name-based]`이다. 같은 이름의 다른 심볼은 빠진다. Go가 아닌 파일은 지금처럼 이름 기반으로 덧붙이고, 그 줄 묶음 앞에 따로 머리를 단다 |
| `defs` | Go 정의는 사실의 `start`/`end`와 패키지를 쓴다. 같은 이름이 여러 패키지에 있으면 패키지 경로를 함께 보여 준다 |
| `callers` (새 op) | 인자 `symbol`, `glob`(선택), `include_tests`(기본 false). `path:line: enclosing  text` 형식으로 그 심볼을 **호출**하는 줄만 준다(`call: true`). 최대 200줄. Go 사실이 없으면 `error: "no type information for this language"`를 돌려주고 `refs`를 쓰라고 안내한다 |
| `callees` (새 op) | 인자 `symbol`. 그 함수·메서드 본문 안에서 호출하는 대상을 정의 위치(`path:start`)와 함께 준다. 최대 200줄. 모듈 밖(표준 라이브러리) 대상은 패키지 경로와 이름만 준다 |

- MCP 도구 `repo_callers`, `repo_callees`를 같은 규칙으로 더한다.
- 프롬프트 안내: "Go의 호출 관계는 callers/callees로 찾고, 인용하기 전에 read로 확인하라."
- **근거 규칙:** `callers`·`callees`는 `defs`처럼 어떤 파일도 제공한 것으로 세지 않는다(구조 검토 결정). 위치만 알려 주므로, 인용하려면 `read`로 원문을 받아야 한다. 타입 검사로 바뀐 `refs`는 지금처럼 grep과 같이 센다. 줄 범위 기반 인용 검증(P2.5)이 들어오면 그 규칙을 따른다.
- 저장소 지도(2a)의 순위도 Go 파일은 사실의 참조 수(같은 이름이 아닌 실제 참조)를 쓴다. 지도 텍스트가 바뀌므로 `MAP_VERSION`을 올린다.

### 실패

추출이 실패해도 토론은 계속된다. 실패한 Go 파일은 1층의 이름 기반 결과로 돌아간다.

| 경우 | 처리 |
|---|---|
| `go`가 없음, `goindex`가 없음 | 추출하지 않는다. 이벤트 한 번, 이후 이 실행에서는 다시 시도하지 않는다 |
| 의존성이 모듈 캐시에 없음(`GOPROXY=off`) | 그 모듈만 실패로 기록한다. 다른 모듈은 계속한다 |
| 타입 오류가 있는 패키지 | 해석된 부분은 쓰고, 풀지 못한 식별자는 `unresolved`에 넣는다. `refs`·`callers` 결과 끝에 "타입 오류로 해석하지 못한 위치 N개"를 적는다(P2.1의 조용한 거짓 음성 대책과 같은 원칙) |
| 제한 시간 초과 | 프로세스 그룹 종료, 실패 기록. 같은 sha는 이 실행에서 다시 시도하지 않는다 |
| 정리 실패(`worktree remove`) | 경고 이벤트. 다음 추출 전 `git worktree prune`과 남은 임시 디렉터리 삭제로 회수한다 |

### 기록

- 이벤트 `compiler_index`: `data {"lang": "go", "sha", "status": "built"|"cached"|"failed"|"timeout"|"unavailable", "packages", "defs", "refs", "unresolved", "ms", "error"}`. 추출이 끝날 때 한 번 낸다.
- 스레드 `context_snapshot.index`에 `go: {"version", "go", "status", "packages"}`를 더한다.
- `retrieval_served.results[]` 항목에 `resolved: "types"|"name"`을 더한다(`refs`·`defs`·`callers`·`callees`).

### 한계

- 인터페이스 호출은 인터페이스 메서드까지만 푼다. 구현으로 이어지는 호출 그래프(포인터 분석)는 하지 않는다. `callers`의 머리에 "인터페이스 경유 호출은 인터페이스 메서드로 표시됨"을 적는다.
- 함수 값, 리플렉션, `go:linkname`은 풀지 않는다.
- 빌드 태그는 허브의 `GOOS/GOARCH`(linux/arm64) 기준이다. 다른 플랫폼 전용 파일은 사실에 없고 1층 결과로 남는다.
- 언어 경계(Go가 `exec.CommandContext`로 `guardian.py`를 부르는 것)는 타입 정보로 이어지지 않는다. 이런 경로는 계속 grep·read로 찾는다(평가 2차 E2가 이 경우를 잰다).

### 다음 언어 (P2.6 범위 밖)

- **TS:** 프로젝트에 lockfile과 이미 설치된 `node_modules`가 있을 때만 `tsc --noEmit` 기반 도우미를 쓴다. 허브가 직접 설치해야 한다면 `npm ci --ignore-scripts`만 허용하고, 그 결정은 따로 받는다.
- **테스트 실행 작업 트리 분리:** 지금 프로젝트 테스트는 작업 클론 안에서 돌아서 산출물(`apps/agent/agent`, `__pycache__`)이 남는다. 같은 임시 작업 트리 방식으로 옮기는 것은 액션 실행 흐름(`MCPClient`, publish 검사)을 바꾸므로 별도 단계로 설계한다.

### 검증 (P2.6)

- `goindex` 단위 테스트(Go): 같은 이름 다른 패키지, 메서드 vs 함수, 인터페이스 호출, 테스트 파일, 타입 오류 패키지, 비밀 경로 제외.
- 엔진 테스트(Python, 실제 `go`가 없으면 건너뜀): 사실 JSON 읽기와 `refs`/`callers`/`callees` 결과, 실패 경로(도우미 없음, 시간 초과), 정리(작업 트리와 임시 디렉터리가 남지 않음, `git worktree list`가 원래대로).
- 미끼 1의 Go 버전: 같은 이름 `Handle`을 두 패키지에 두고 하나만 호출할 때 `refs`·`callers`가 호출되는 쪽만 보여 준다.
- 허브 측정: server-guardian 기준 추출 시간(처음, 같은 sha 재사용), `GOCACHE` 크기, 피크 메모리.
- 평가 2차에서 B1~B3·E2의 정답 일치가 P2.1보다 같거나 낫고, Go 대상 질문에서 grep 요청이 줄어야 한다.

### 구현 메모 (P2.6)

구현: `tools/goindex/`(Go 도우미, 표준 라이브러리만), `cli/llm-agent/go_facts.py`(사실 읽기, 추출, 저장, 대기), `go_ops.py`(`refs`/`defs`/`callers`/`callees` 본문), `repo_tools.py`·`repo_map.py`·`repo_mcp.py`·`debate_engine.py`의 연결. 설계와 다르게 구현했거나 설계에 없던 결정이다.

**사실 형식과 도우미**

- 도우미 인자는 `-root -module(반복) -readable -out`에 `-sha`(출력에 그대로 적음)와 `-timeout`(0이면 제한 없음)을 더했다. 종료 코드는 0(사실 기록), 1(색인한 모듈이 없음, `module_errors`가 든 사실은 그래도 기록), 2(사용법·입출력 오류), 3(시간 초과, 아무것도 쓰지 않음)이다. 엔진은 자기 제한 시간보다 30초 길게 `-timeout`을 넘겨, 엔진이 죽어 고아가 된 도우미도 스스로 끝나게 한다.
- `-readable`에는 읽을 수 있는 `.go` 파일만 넘긴다(비 Go 파일은 사실에 나오지 않으므로).
- `modules`는 설계처럼 색인에 성공한 디렉터리 문자열 목록이고, 실패한 모듈은 `module_errors: [{dir, error}]`에 따로 둔다. `files`(타입 검사해 보고한 `.go` 파일)를 더했다. 엔진은 이 목록에 없는 Go 파일(빌드 태그나 다른 플랫폼 전용 파일)을 1층의 이름 기반 결과로 계속 다룬다. `go`, `goos`, `goarch`, `sha`도 적는다.
- `kind`는 1층과 같은 이름(`function` `method` `struct` `interface` `type` `var` `const`)이다. 인터페이스에 선언된 메서드는 `Iface.Method`라는 `method` 정의이고 인터페이스 호출은 거기를 가리킨다. 필드, 매개변수, 지역 변수는 정의가 아니다. `init`과 `_`는 뺀다.
- 사실에는 서명이 없다. `defs`는 같은 위치(`path`, `start`)의 1층 선언에서 서명(리터럴을 가린 첫 줄)을 가져오고, 없으면 정규화 이름을 적는다.
- 모듈 밖(표준 라이브러리, 의존성)의 호출 대상은 `external: true` 정의(경로·줄 없음)와 `call: true` 참조로만 남긴다. 호출하는 함수·대상 쌍마다 하나만 남기므로(첫 호출 줄) `callees`용이며 `refs`/`callers`는 모듈 안 정의만 다룬다. 모듈 안이지만 읽을 수 없는 파일(비밀 이름)에 정의된 함수를 부르는 호출은 외부 대상으로도 남기지 않는다.
- 같은 선언을 여러 번 검사해도(패키지와 그 테스트 변형) 정의는 이름 위치(`파일:줄:열`)로 한 번만 나온다. 테스트 변형은 `_test.go` 파일의 정의·참조만 보고하고, `go list -test`의 생성 테스트 main은 건너뛴다. 모듈 안 패키지는 `go list`의 export data가 아니라 소스로 타입 검사하므로(그래야 타입 오류가 있는 패키지도 의존하는 쪽에 타입이 남는다) 표준 라이브러리와 의존성만 export data를 쓴다.
- 풀지 못한 식별자(`unresolved`)는 타입 오류가 있는 패키지에서 `Defs`·`Uses`에 없는 식별자(`_`, 패키지 이름, 레이블 제외)다. 오류 메시지 본문은 소스 조각을 담을 수 있어 저장하지 않고, `packages[].errors`에는 `경로:줄`만 남긴다(읽을 수 없는 파일의 오류는 위치도 뺀다). `unresolved`는 5000개까지다.
- 환경은 설계의 네 값에 `GOWORK=off`를 더하고, `go list`에 `-buildvcs=false`를 준다. `testdata` 아래 `go.mod`는 모듈로 치지 않는다(`MCPClient._go_modules`의 규칙에 더한 것).

**엔진**

- `GoFactsProvider`는 `RepoTools`가 `cache_dir`을 받으면 만든다(`<cache_dir>/go/`가 저장소, 빌드 캐시는 `<GUARDIAN_DATA_DIR>/code-index/.gocache`). 추출을 시작하는 것은 엔진의 `_setup_retrieval`뿐이고(`start()`), repo MCP 서버 프로세스는 같은 클래스를 쓰되 저장된 파일만 읽는다. 그 파일이 없고 다른 프로세스가 `<sha>.lock`을 쥐고 있으면 최대 `GUARDIAN_GOINDEX_WAIT`초 기다린다.
- 추출이 끝나면 `<sha>.status.json`(상태, 오류, 시간)을 남긴다. 읽기 프로세스는 이것으로 "failed/timeout/unavailable"을 안내하고, 새 추출은 시작할 때 지운다. 캐시 적중에는 쓰지 않는다. 사실 파일은 적중할 때마다 mtime을 갱신하고(최근 20개 규칙은 그 mtime 기준), 지울 때 `.status.json`과 `.lock`도 함께 지운다.
- 기다리는 것은 요청이 처음 추출 중인 상태를 만났을 때 한 번뿐이다. 그 뒤 요청은 기다리지 않고 바로 이름 기반 결과와 머리 안내를 준다. 기다리다 끝나지 않으면 다음 요청에서 끝나 있으면 그때부터 타입 검사 결과를 준다.
- 토론이 끝나면 아직 도는 추출을 취소한다(도우미 프로세스 그룹을 죽이고 작업 트리와 임시 디렉터리를 지운다). 그래서 매우 짧은 토론은 추출을 끝내지 못하고, 미리 데우는 `GUARDIAN_GOINDEX_PREFETCH`는 만들지 않았다.
- 임시 디렉터리는 `guardian-goindex-` 접두사이고, 추출 전에 한 시간 넘은 것을 지운 뒤 `git worktree prune`을 돈다. `git worktree add`에는 `core.hooksPath=/dev/null`을 줘서 저장소의 hook이 돌지 않게 했다. 작업 클론에서 바뀌는 것은 `.git/worktrees/`뿐이다.
- 정리 실패(`worktree remove` 실패 또는 임시 디렉터리 삭제 실패)는 별도 이벤트 `compiler_index_warning {lang, sha, message}`로 알린다. 설계의 "경고 이벤트"다.
- `compiler_index`는 `debate_started` 뒤에 나오도록 붙잡았다가 내보낸다(캐시 적중은 `_setup_retrieval` 안에서 일어나므로 그렇지 않으면 스레드 id가 없고 순서가 앞선다). 이벤트의 `defs`는 모듈 안 정의 수(외부 호출 대상 제외)이고 `packages`는 서로 다른 패키지 경로 수다.
- 스냅샷 `index.go`는 `{version, go, status, packages}`이며 `index`가 있을 때(`GUARDIAN_REPO_MAP`이 켜져 있을 때)만 기록한다. 설정 시점과 실행이 끝날 때 두 번 갱신하므로 끝났을 때 아직 추출 중이면 `pending`이다.
- 테스트 격리: `testenv.isolate_data_dir()`가 `GUARDIAN_GOINDEX_BIN`을 없는 파일로 바꾼다(이 저장소에도 `go.mod`가 있어, 도우미가 있는 허브에서 단위 테스트가 실제 추출을 시작하지 않게 한다). `test_debate_engine.py`도 같은 값을 준다.
- `GUARDIAN_GOINDEX_MAP_WAIT`(기본 0초)를 더했다. 첫 프롬프트의 저장소 지도는 이 시간만 추출을 기다린다. 0이면 사실이 이미 있을 때(캐시 적중)만 쓰고, 아니면 이름 기반 지도에 안내 줄을 붙인다.

**op**

- Go 대상 판정: 사실이 이미 읽혀 있으면 사실에 그 심볼의 정의가 있을 때. 읽혀 있지 않으면 1층이 `.go` 파일에서 그 심볼의 선언을 찾았거나 `glob`이 `.go`로 끝날 때만 사실을 기다린다(그 밖의 심볼은 기다리지 않고 이름 기반). `callers`/`callees`는 항상 기다린다.
- `refs`(사실이 있을 때): 머리는 설계대로이고, 같은 이름의 정의가 둘 이상이면 둘째 줄에 `[N definitions match this name (packages: …); references to all of them are listed]`를 적는다(경로는 적지 않는다. 결과에 나온 경로는 언급으로 센다). 패키지로 심볼을 한정하는 문법은 없으므로 `Handle`은 모든 패키지의 `Handle`이다. 그 아래 타입 검사된 `경로:줄: 텍스트` 줄, 이어서 사실에 없는 파일(비 Go 파일, 타입 검사하지 않은 Go 파일)의 이름 기반 줄이 자기 머리 아래 나온다. 상한 200줄은 둘을 합쳐 센다. `include_defs`는 정의 줄을 타입 검사 쪽에 더한다.
- 끝 표시: 타입 오류로 풀지 못한 위치는 P2.1 색인 범위 줄에 합쳐진다(`[index coverage: 1 location unresolved due to type errors — results may be incomplete; use grep for these: 경로]`). 그래서 `provided_paths`/`mentioned_in`이 무시하는 줄이다. 식별자 이름이 같은 `unresolved`만, `glob`이 있으면 그 안의 것만 센다.
- `defs`(사실이 있을 때): `[defs of X: type-checked (go…)]` 머리, 정의 줄(여러 패키지면 줄 끝에 `(package …)`), 그리고 사실에 없는 파일의 이름 기반 줄이 `[defs of X in files without type information: name-based]` 아래 나온다. 사실이 없으면 이전과 같다.
- `callers`: `경로:줄: 감싸는함수  줄 텍스트`(패키지 수준이면 `(package level)`), 정의가 여럿이면 머리 아래 `matched N definitions:`와 정의 위치. `include_tests` 없이 결과가 없으면 테스트를 포함하라고 안내한다. `callees`: 모듈 안 대상은 `경로:시작  이름  (called at line N)`, 밖은 `패키지경로.이름  (called at …)`, 처음 호출한 줄 순서. 둘 다 200줄 상한. 사실이 없으면 오류(`no type information for this language (Go only); use refs`, 준비 중·실패면 `no type information yet (Go index <상태>); use refs`).
- `resolved`는 결과 항목에 `"types"` 또는 `"name"`으로 들어간다(`refs`/`defs`는 사실을 쓰면 `types`, 아니면 `name`). 그래서 `RepoTools.run`의 결과 키가 둘 이상 늘고 이벤트 `retrieval_served.results[]`와 MCP 서버 로그에도 있다. 실패한 호출에는 없다.
- 이름 기반 대체 결과의 안내 줄: `[type information for Go is still being prepared: results are name-based]`(추출 중), `[no type information for Go (failed|timeout|unavailable): results are name-based]`. 추출을 시도한 적이 없는 읽기 프로세스는 아무것도 적지 않는다.
- 인용 검증: `callers`/`callees`는 `defs`와 같이 어떤 파일도 제공한 것으로 세지 않고 줄 범위도 만들지 않으며 `mentioned_in`도 비운다. `refs`는 타입 검사된 줄이 보인 줄이다.
- 프롬프트: 탐색 안내에 `callers`/`callees` 설명과 예시 요청, "Go 결과는 첫 줄에 `type-checked`가 있을 때만 타입 검사 결과"라는 문장을 더했다. `RETRIEVAL_PROMPT_VERSION`은 `2026-10-12.p2.6`이다. MCP 도구는 `repo_callers`(`symbol`, `glob`, `include_tests`)와 `repo_callees`(`symbol`)이고 도구 목록 끝에 붙는다.

**지도**

- 사실이 있으면 사실에 정의가 있는 Go 선언은 선언 파일 밖의, 잡음 경로가 아닌 서로 다른 참조 파일 수로 순위를 매긴다(길이 2 이하 이름 제외 규칙은 타입 정보가 있어 적용하지 않는다). 선언 파일이 잡음 경로면 0이다. `MAP_VERSION`은 3이다.
- Go 파일이 있는 디렉터리의 지도에는 머리 아래 한 줄이 붙는다: 사실을 썼으면 `[Go files ranked by type-checked references (go1.26.8)]`, 아니면 위의 이름 기반 안내 줄. Go 모듈이 없는 저장소의 지도는 이전과 같다.

**설치와 CI**

- `scripts/install-agent-service.sh`는 agent와 같은 방식(`GOOS=linux GOARCH=<허브> CGO_ENABLED=0`)으로 `tools/goindex`를 교차 빌드해 `~/.local/libexec/guardian/goindex`에 임시 이름으로 올린 뒤 `mv`로 바꿔 둔다(755). 이 단계는 선택 기능이므로 작업 클론·프로젝트 동기화 단계처럼 실패하면 경고하고 설치를 계속한다. `agent.env` 예시에 `GUARDIAN_GOINDEX_*`를 더했다.
- `scripts/ci.sh go`는 `tools/goindex`에 gofmt, `go vet`, `go test -race`, linux arm64/amd64 빌드를 더한다.

**테스트**

- `tools/goindex/index_test.go`(Go 14개): 같은 이름 두 패키지, 메서드와 함수, 인터페이스 호출, 테스트 파일, 타입 오류 패키지의 부분 결과, 읽을 수 있는 파일만, 외부 호출 대상, 감싸는 함수, 묶음 선언, 환경 강제, 시간 초과 종료 코드 등.
- `cli/llm-agent/test_go_facts.py`: 사실 조회, 보관 개수, 가짜 도우미로 추출 흐름(적중, 다른 go 버전, 다른 도우미 버전, 부분 모듈 실패, 실패, 재시도 안 함, 시간 초과의 프로세스 그룹 종료, 취소, 도우미·go 없음, 정리 실패, 동시 실행 공유, 빌드 캐시 정리, 환경에 비밀 없음), 대기와 대체 결과, 읽기 프로세스, 각 op의 출력, 지도, 엔진 이벤트·스냅샷, 그리고 실제 도우미를 `go build`로 만들어 임시 저장소를 색인하는 통합 테스트(`go`가 없으면 건너뜀)와 미끼 1의 Go 버전.
- 기대값을 의도적으로 바꾼 기존 테스트: `test_evidence.py`(프롬프트 버전), `test_llm_runner.py`·`test_repo_mcp.py`(도구 목록에 두 도구 추가), `test_repo_index_ops.py`(`defs` 결과에 `resolved` 키).

**측정**(이 저장소 HEAD `f86f986`, `apps/agent` 모듈 하나, 이 Mac arm64 / Go 1.26.8, 새 빌드 캐시)

| 항목 | 값 |
|---|---|
| 추출 시간, 처음(빈 `GOCACHE`) | 5.7초(도우미 5.2초) |
| 추출 시간, 빌드 캐시만 있음(사실 파일 없음) | 1.0초(도우미 0.33초, 나머지는 작업 트리 만들기·지우기) |
| 같은 sha 재사용(사실 파일 적중) | 0.01초 |
| 사실 JSON | 792,364바이트(정의 727개 + 외부 호출 대상 258개, 참조 4,911개) |
| `GOCACHE` | 약 125 MiB(130,634,186바이트), 표준 라이브러리 컴파일 결과가 대부분 |
| 피크 메모리(도우미와 자식 중 최대 RSS) | 처음 333 MB, 캐시가 있을 때 41 MB |
| 지도 생성(사실 사용, 색인 메모리) | 0.036초, 32,480바이트 |

허브(arm64, 느린 CPU)에서는 처음 실행이 더 오래 걸릴 수 있다. 첫 배포 때 허브에서 다시 재야 한다.

`callers`(`debateHub.followEvents`):

```
[callers of debateHub.followEvents: type-checked (go1.26.8); a call through an interface is listed under the interface method]
apps/agent/debate_events.go:137: debateHub.debateEvents  h.followEvents(w, r, id, after)
```

`callees`(`debateHub.startDebate`, 앞 부분):

```
[callees of debateHub.startDebate: type-checked (go1.26.8); standard library and dependency targets are shown as package.Name]
encoding/json.Decoder.Decode  (called at line 544)
net/http.MaxBytesReader  (called at line 544)
encoding/json.NewDecoder  (called at line 544)
errors.As  (called at line 546)
apps/agent/debate_handler.go:330  errJSON  (called at lines 547, 550, 554, 558, 603, 610, 620)
apps/agent/debate_handler.go:492  debateRequest.validate  (called at line 553)
apps/agent/debate_handler.go:632  debateHub.publishedRefusal  (called at line 557)
apps/agent/debate_handler.go:536  newJobID  (called at line 563)
...
apps/agent/debate_handler.go:659  debateHub.launch  (called at line 616)
apps/agent/main.go:21  jsonResponse  (called at line 626)
```

**알려진 한계**

- Go 빌드 캐시는 `code-index/.gocache`다. `default_cache_dir`이 프로젝트 id 앞의 `.`을 지우므로 어떤 프로젝트의 인덱스 캐시 디렉터리와도 겹치지 않는다.
- 심볼을 패키지로 한정할 수 없다(`a.Handle` 문법 없음). 같은 이름의 정의가 여럿이면 `refs`/`callers`는 모두의 결과를 합치고, 머리에 패키지 목록을 적는다. 미끼 1의 기대는 "안 불리는 쪽의 줄이 나오지 않는다"로 충족된다.
- 취소와 `kill -9`: 엔진이 강제 종료되면 작업 트리 항목이 `.git/worktrees/`에 남는다. 임시 디렉터리를 한 시간 뒤 다음 추출이 지우고 `git worktree prune`이 항목을 회수한다.

## 2a. 저장소 지도

인덱스에서 결정적으로 만든다(같은 sha, 같은 버전이면 같은 텍스트).

```
--- Repository map (sha 3eda3e5, 479 files, ranked by references) ---
cli/llm-agent/
  debate_engine.py  [1702 lines] DebateEngine, DebateEngine.run, DebateEngine._call, retrieval_enabled, …
  repo_tools.py     [538 lines]  RepoTools, RepoTools.run, provided_paths, parse_repo_blocks, …
apps/agent/
  main.go           […] main, guard, …
docs/design/  (32 files)
```

- 파일마다 선언을 **역참조 수**(그 이름이 나온 다른 파일의 수, P2.1부터 서로 다른 파일 수) 순으로 최대 6개 적는다. 선언이 없는 파일은 경로와 줄 수만 적는다.
- 같은 디렉터리의 비코드 파일(문서, 이미지)은 `(N files)`로 접는다.
- 첫 프롬프트: 지금의 파일 목록 자리를 지도로 바꾼다. 크기 상한은 `GUARDIAN_REPO_MAP_BYTES`(기본 32KB)이다. 넘으면 역참조가 적은 파일의 심볼부터 빼고, 그래도 넘으면 깊은 디렉터리를 접는다. 접은 부분은 `map` op로 펼치라고 적는다.
  이 상한은 프롬프트 크기 규칙이며, 2026-10-09에 무제한으로 정한 탐색 예산(`GUARDIAN_RETRIEVAL_*`)과는 별개다.
- 참고 코드 경로가 있는 토론(`pack.snapshot["paths"]`가 비지 않음)은 지금처럼 목록을 넣지 않는다. 대신 지도의 루트 요약(디렉터리별 한 줄)만 넣는다.
- `GUARDIAN_REPO_MAP=0`이면 예전 파일 목록으로 돌아간다(비교 실험용).

### 구현 메모 (P2)

구현: `cli/llm-agent/repo_map.py`(`build_map`, `root_summary`, `repo_map_cap`, `repo_map_enabled`, `MAP_VERSION`). 설계와 다르게 구현했거나 설계에 없던 결정이다.

- **`INDEX_VERSION`은 1 그대로다.** 줄 수가 필요해서 blob 캐시 JSON에 `lines`가 늘었다. `lines`가 없는 P1 캐시 파일은 형식 검사에서 무효가 되어 다시 파싱한다(버전 변경 없이). 캐시 JSON은 소스 본문이 아니라 선언 첫 줄(`signature`, 200자까지)과 식별자 출현, 줄 수만 담는다.
- **비코드 파일 접기의 정의.** `code_index.language`가 파서를 가진 확장자(.go .py .ts .tsx .js .jsx .mjs .cjs)가 코드 파일이고 그 밖의 읽을 수 있는 파일은 비코드다. 코드 파일이 있는 디렉터리에서는 `  (+N non-code files)` 한 줄로, 비코드뿐인 디렉터리는 `dir/  (N files)` 한 줄로 센다. N은 그 디렉터리 바로 아래 파일 수다. 1MB를 넘어 색인하지 않은 코드 파일은 `[large file, not indexed]`로 나온다.
- **역참조 수.** (P2.1에서 바뀜: 서로 다른 파일 수, 노이즈 경로 제외. 아래 "구현 메모 (P2.1)".) 선언 식별자(정규화 이름의 마지막 성분)가 선언 파일이 아닌 다른 파일에서 나온 "줄 수"의 합이다. 동률은 정규화 이름, 파일 간 순위는 선언 중 최대값과 경로로 깬다. 선언이 없는 파일은 순위에 들지 않는다.
- **상한 처리 순서.** 심볼 제거(순위 낮은 파일부터, 파일은 `…`만 남김) 후에도 넘으면 가장 깊은 디렉터리 한 단계씩 `dir/  (N files, folded)`로 접는다. 접은 뒤 남는 여유가 있으면 순위 높은 파일의 심볼을 다시 준다(구현은 단계마다 상한에 들어가는 가장 큰 K를 고른다). 심볼을 줄이거나 접으면 마지막에 `map` op로 펼치라는 안내 한 줄(약 110바이트)이 붙는다. 그래서 줄이는 이득이 이 줄보다 작으면 심볼을 빼는 쪽이 오히려 커져 한 단계 더 접는다. 가장 작은 배치는 `(all N files, folded)` 한 줄이고, 상한이 이보다 작으면 그대로 넘긴다. 모든 단계에서 읽을 수 있는 모든 파일은 목록 또는 개수로 나온다.
- **`map` op.** `depth` D는 요청한 `path`를 0단계로 보고 D-1단계까지 자세히 보이며, D단계 디렉터리와 그 아래는 `dir/  (N files, folded)`로 센다(N은 아래 전체). `list`처럼 `depth`를 1~4로 자르고 정수가 아니면 기본 2이다. 크기 상한 `GUARDIAN_REPO_MAP_BYTES`는 첫 프롬프트에만 적용하고 `map` op는 일반 결과 한도(`max_bytes`)만 받는다. 머리의 파일 수는 읽을 수 있는 전체 파일(색인한 코드 파일 수가 아님)이다.
- **루트 요약.** 참고 코드 경로가 있는 토론에는 `--- Repository summary (sha …, N files; …) ---` 아래에 최상위 디렉터리마다 `name/  N files; top symbols: a, b, c`(루트 바로 아래 파일은 `./`)를 넣는다. 크기 상한은 두지 않는다(최상위 디렉터리 수만큼의 줄). 코드 묶음 자신의 `Repository file list`는 그대로다.
- **색인은 토론 시작 때 만든다.** 지도가 켜져 있으면 `_setup_retrieval`이 색인을 바로 만든다(`defs`/`refs`를 기다리지 않음). 색인이나 지도 생성이 실패하면 경고만 찍고 예전 파일 목록으로 돌아가며 토론은 계속된다.
- **기록.** 스레드 `context_snapshot.index`는 `{sha, version, files, symbols, prompt_version, map_version}`(`files`는 색인한 코드 파일 수)이고, 지도를 끄면 색인을 만들지 않으므로 키가 없다. `debate_started.data.context`에 `map_bytes`(프롬프트에 넣은 지도·요약 바이트, 끄면 0), `prompt_version`(`RETRIEVAL_PROMPT_VERSION`, 탐색 프롬프트 문구를 바꾸면 올린다), `map_version`(`MAP_VERSION`, 끄면 null)이 늘었다. 이벤트 표(`debate-agent-retrieval.md`)에 반영했다.
- sha 단위 JSON과 20개 sha 보관은 P2에서도 만들지 않았다(지도는 호출 때마다 메모리에서 만든다).

측정(이 저장소 HEAD `f2ef643`, 읽을 수 있는 파일 494개, 색인한 코드 파일 299개, 선언 3,637개, 임시 캐시 디렉터리):

| 항목 | 값 |
|---|---|
| 상한 없는 전체 지도 | 32,115바이트 |
| 32KB 상한 지도 | 32,115바이트(상한 안이라 전체와 같다, 접힘 0) |
| 상한 16KB / 8KB / 4KB | 16,316 / 8,185 / 1,591바이트(8KB에서 12개 디렉터리를 접음, 4KB는 최상위 디렉터리만 자세히) |
| 루트 요약 | 541바이트 |
| 생성 시간(색인 포함) | 콜드 0.83초, 웜(blob 캐시 사용) 0.10초, 색인이 메모리에 있을 때 지도만 0.007초 |
| `map` op 기본(루트, depth 2) | 1,591바이트 |

- 이 저장소는 전체 지도가 32KB 바로 아래라 기본 상한에 걸리지 않는다. 그보다 큰 저장소에서만 접힘이 일어난다.
- 관찰: 역참조 수가 이름 기반이라 TSX 데모 파일의 지역 상수(`row`, `item`, `col`)와 스토리 이름이 위로 올라온다. 순위 품질은 평가 1차에서 본다.

### 구현 메모 (P2.1, 견고성)

구조 검토(`docs/analysis/2026-10-12-retrieval-architecture-review.md`)의 W2·W4·W4b·W5와 평가 1차의 지도 순위 잡음, 캐시 수명을 한 번에 넣었다. 에이전트가 보는 글이 바뀌므로 평가 1차 뒤에 넣었고, `INDEX_VERSION`은 2, `MAP_VERSION`은 2가 되었다.

- **조용한 거짓 음성 표시(W2).** `defs`/`refs` 결과의 마지막 줄에 `[index coverage: N files failed to parse, M files over 1 MB not indexed, K files with this identifier capped at 5000 occurrences — results may be incomplete; use grep for these: 경로, …]`를 붙인다(`code_index.coverage_note`). 개수가 0인 항목은 빼고 셋 다 0이면 줄이 없다. 요청에 `glob`이 있으면 그 glob에 맞는 파일만 센다. 경로는 최대 10개이고 더 있으면 `(+N more)`다. "참조/정의 없음" 결과에도 붙는다(거짓 음성이 가장 위험한 경우). 상한 도달은 `refs`에만 해당하며 식별자 하나를 보므로 개수는 "그 식별자가 상한에 닿은 파일 수"다(캐시에 따로 저장하지 않고, 파일 안 출현 줄 수가 5000이면 상한으로 본다). `map` op와 첫 프롬프트 지도는 같은 줄을 머리 바로 아래에 둔다(요청한 하위 트리의 파일만 센다, 상한 계산에 포함). 이 줄의 경로는 `provided_paths`/`mentioned_in`이 무시한다(에이전트가 본 코드도, 본문에서 언급된 경로도 아니다).
- **캐시 내용(W4).** 선언 `signature`는 저장 전에 문자열 리터럴 내용(Python 따옴표·삼중따옴표·`r`/`b`/`f`/`u` 접두사, Go 큰따옴표·룬·백틱, TS 따옴표·백틱)을 `"…"`로 바꾼다. 닫히지 않은 리터럴은 줄 끝까지 가린다. 줄 끝 주석은 그대로다. 그래서 `defs` 출력의 서명은 가려질 수 있고 `read`는 실제 코드를 보여 준다. 코드 컨텍스트가 쓰는 `outline`은 바뀌지 않는다: Go·Python 개요는 캐시를 쓰지 않고 원본 줄에서 만들며, TS 개요(`ts_outline`)는 원본 줄로 만든다(캐시용 선언만 가림). `INDEX_VERSION`을 올려 리터럴이 남은 이전 캐시 파일은 다시 파싱하고, 정리(`prune_cache`)가 다른 버전의 캐시 파일을 나이와 무관하게 바로 지운다. `RETRIEVAL_PROMPT_VERSION`은 에이전트가 보는 글(지도, 결과 끝 표시)이 바뀌었으므로 `2026-10-12.p2.1`로 올렸다.
- **`refs`의 줄 읽기(W5).** 줄 내용을 파일마다 `git cat-file`로 읽지 않고 `RepoTools._read_blobs`(`git cat-file --batch`)로 25개 파일씩 읽는다. 결과 텍스트는 같다.
- **비밀 필터(W4b).** `code_context.is_secret`은 `SECRET_PATTERNS`를 경로의 모든 성분(디렉터리 포함)에 대소문자 구분 없이 맞추고, 디렉터리로만 `.ssh` `.gnupg` `.aws` `.docker` `.kube`를 막는다(그 아래 전부 비밀). 내용 검사는 하지 않는다. 호출처는 코드 묶음, `RepoTools.readable_paths`/`_excluded`, `mcp_client`의 편집 막기이며, 편집 막기는 작업 공간 기준 상대 경로(해석한 경로)를 보므로 작업 공간 위의 디렉터리 이름(`~/secrets-projects/…`)이 모든 파일을 막지는 않는다. 이 저장소 HEAD(`c677431`)의 추적 파일 503개 중 읽기에서 새로 제외되는 파일은 없다(`*secret*`·`*credential*` 이름은 이미 파일 이름 검사로 제외되어 있었다).
- **`retrieval_warning` 이벤트.** 한 탐색 단계(repo 블록 한 번 또는 MCP 호출 한 번)의 제공 바이트가 `GUARDIAN_RETRIEVAL_WARN_BYTES`(기본 256 KiB, 0이면 끔. 처음 2 MiB였다가 허브 위키 작성의 압축 사고 뒤 낮췄다)를 넘으면 `retrieval_served` 바로 뒤에 `{step, bytes, threshold, via}`를 낸다. 한도를 두지 않는다(예산은 무제한 그대로). 대시보드 표시는 하지 않는다: Agents 모니터는 모르는 이벤트 종류를 건너뛴다(`agentMonitor.ts`의 reducer `default`).
- **지도 순위(평가 1차 관찰 1).** 역참조 수는 그 이름이 나온 *서로 다른 파일 수*(선언 파일 제외)다. 스토리·미리보기·테스트 성격의 경로(`*.stories.*`, `previews/`, `.design-sync/`, `__tests__/`, `*_test.go`, `test_*.py`, `*.test.ts(x)`/`.js(x)`)에서 나온 참조는 세지 않는다. 이름 길이가 2 이하인 식별자는 순위를 매기지 않는다. 같은 경로 집합 안의 선언도 순위를 매기지 않는다(미리보기 상수 `nodes`가 다른 파일에서 쓰인다고 중요한 심볼은 아니다). 다른 파일에서 한 번도 참조되지 않는 선언은 순위 없음으로, 순위 있는 선언 뒤에 정규화 이름 순으로 나온다. 이 저장소에서 `.design-sync/previews/*.tsx`의 `actions, nodes, commands`, `row, item, col`, 스토리 이름이 위로 올라오던 현상이 사라진다.
- **캐시 수명.** 위 "캐시" 절의 수명 규칙을 `code_index.prune_cache`가 구현한다. 지울 대상은 `blobs/*/*.json`과 한 시간 넘은 `.tmp-*`뿐이고 빈 샤드 디렉터리는 함께 지운다.
- **테스트.** `test_code_index.py`(가림, 버전, 수명), `test_repo_index_ops.py`(끝 표시의 모든 경우, 일괄 읽기), `test_repo_map.py`(순위, 지도 표시), `test_code_context.py`·`test_patch_ops.py`(비밀 경로), `test_db_backup.py`(백업 제외), `test_retrieval_robustness.py`(경고 이벤트).

### 구현 메모 (P2.5, 근거)

줄 단위 인용 검증과 선택 `findings` 블록(`cli/llm-agent/evidence.py`). 설계와 규칙의 전부는 `docs/design/debate-agent-retrieval.md`의 "구현 메모 (P2.5, 근거)"에 있고, 이 문서와 관련된 점만 적는다.

- **`defs`/`map`/`list`는 여전히 코드 근거가 아니다.** 그 결과는 보인 줄 범위를 만들지 않으므로 `defs`가 알려 준 `path:6-8`만 보고 `path:7`을 인용하면 받은 적 없는 코드로 되묻는다. `refs`/`grep`은 일치한 줄만 보인 범위로 센다(같은 이름의 `Handle`이 있는 다른 파일의 줄은 해당하지 않는다). 그래서 `refs` 한 번으로 파일 200개가 인용 가능해지던 W3은 사라진다.
- **새 `INDEX_VERSION`은 없다.** 인덱스 캐시는 바뀌지 않는다. `RETRIEVAL_PROMPT_VERSION`이 `2026-10-12.p2.5`가 된다.
- **P2.6의 `callers`/`callees`**는 지금과 같이 보인 줄을 만들지 않는 op이다(근거로 세지 않는다). P3 위키도 같다: 위키 텍스트는 보인 줄이 아니다.
- **평가 2차**: `unverified` 수는 이제 줄 단위 어긋남을 포함하고, `retrieval.findings`로 provider별 findings 제출률과 불일치 수를 읽는다.

## 2층. 코드 위키

### 페이지

페이지 하나는 디렉터리 하나(`cli/llm-agent`) 또는 이름 붙은 기능(`feature:debate-retrieval`)을 다룬다.
(2026-10-18: 기능 페이지 id는 `feature-<문서 이름>`이고, 페이지 종류는 `dir`, `overview`, `group`, `feature` 넷이다. `code-wiki-coverage.md`.)

범위 확장(설계 문서별 기능 페이지, 큰 디렉터리 나누기, 파일 보기, 모두 구현됨): `code-wiki-coverage.md`.

저장 위치는 허브 데이터 디렉터리 `<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages/<page id>.md`이다(대상 프로젝트 저장소에는 쓰지 않는다. 아래 "결정" 1).

```markdown
---
id: cli-llm-agent
title: 토론 엔진 (cli/llm-agent)
scope: ["cli/llm-agent/"]
written_sha: 3eda3e5…
basis:                       # 서술을 쓸 때 실제로 읽은 파일과 그때의 blob id
  cli/llm-agent/debate_engine.py: 9c1f…
  cli/llm-agent/repo_tools.py: 41ab…
author: {kind: llm, provider: claude, job: kb_20261012_…}   # 또는 {kind: human, user: …}
updated: 2026-10-12T09:00:00Z
---
## 역할
...
## 흐름
...(주장마다 `cli/llm-agent/debate_engine.py:845` 같은 인용)
## 주의점
...
## 관련 페이지
[[feature-debate-retrieval]]
```

- 본문은 서술 영역(2b)만 담는다. 생성 영역(2a)은 파일에 저장하지 않고 페이지를 열 때 현재 sha로 만든다. 내용은 `scope`의 지도와, 이 페이지의 인용을 현재 sha에서 검사한 결과다.
- `basis`는 쓰는 사람이 실제로 읽은 파일이다. LLM 작성이면 그 세션의 `provided_files`에서 자동으로 채우고, 사람 작성이면 본문의 강한 인용 경로에서 채운다.

### 낡음 판정

토론 sha에서 페이지를 열 때마다 계산한다(저장하지 않는다).

| 상태 | 조건 | 처리 |
|---|---|---|
| `fresh` | `basis`의 모든 blob id가 현재와 같고, 본문의 모든 인용 경로가 현재 트리에 있다 | 그대로 보여 준다 |
| `stale` | `basis` 파일 중 바뀐 것이 있다 | 머리에 "다음 파일이 이 설명을 쓴 뒤 바뀌었다: …" 경고를 붙여 보여 준다 |
| `broken` | 인용한 경로가 없어졌거나, `path#Symbol` 인용의 심볼이 현재 인덱스에 없다 | 경고와 함께 깨진 인용 목록을 붙인다 |

`scope` 안에 `basis`에 없는 새 파일이 생겨도 `stale`로 본다("설명이 다루지 않는 파일: …"). `stale`·`broken` 페이지는 첫 프롬프트에 넣지 않고 `wiki` op로만 준다.

### 에이전트에게 노출

- `wiki` op: `query`는 페이지 제목·scope·본문을 대소문자 무시 부분 문자열로 찾아 `id, title, state, scope` 목록(최대 20개)을 준다. `page`는 위 형태의 페이지 하나를 준다.
- 첫 프롬프트: `fresh`인 페이지가 있으면 지도 뒤에 `id — title (scope)` 목록(최대 40줄)을 넣는다. 본문은 넣지 않는다. (2026-10-18: 목록은 `overview`·`dir`, `feature`, `group` 순서다. `code-wiki-coverage.md`.)
- 안내문: "위키는 길잡이다. 위키 내용은 근거가 아니며, 주장은 직접 읽은 코드로 인용하라. 위키가 코드와 다르면 코드가 맞다."
- 결과 텍스트에 머리 `[wiki page … — navigation notes, not evidence; state: fresh]`를 붙인다.

### 작성과 갱신

위키 서술도 지금의 탐색 루프와 인용 검증을 그대로 쓰는 LLM 작업으로 만든다. 별도의 요약 경로를 만들지 않는다.

- 작업 종류 `code_wiki`: `guardian.py code-wiki build --project P [--page ID | --stale | --all] [--sha S]`(2026-10-18: 실제 명령은 `--code-wiki-build`이다. 아래 "구현 메모 (P3)"). 허브 Go API는 토론 작업처럼 job 디렉터리를 만들고 실행한다. 이벤트는 같은 `events.jsonl` 형식이며 `type: wiki_page_written {page, state, basis_files, citations, unverified}`를 더한다.
- 한 페이지 = 한 세션. 프롬프트는 지도(그 scope), 기존 서술(있으면, "고칠 부분만 고쳐라"), 작성 규칙(형식, 주장마다 인용, 읽지 않은 코드 금지)이다. 에이전트는 repo 블록·MCP로 읽고 마지막에 본문을 낸다.
- 최종 본문에 지금의 인용 검증을 돌린다. 미검증 인용이 있으면 한 번 되묻는다. 그래도 남으면 해당 문장을 버리지 않고 페이지를 쓰되 `unverified` 목록을 front matter에 남기고 `broken` 상태로 둔다.
- 모델 선택은 팀 설정을 재사용한다(`--team`, 기본은 프로젝트 기본 팀의 Expert 멤버).
- 사람이 쓴 페이지도 같은 파일 형식이다. 대시보드 편집기에서 저장하면 `author.kind: human`이 되고 `basis`는 본문 인용에서 채운다. LLM 재작성은 사람 페이지를 덮어쓰지 않고 `stale` 표시만 한다(`--force`로만 덮어쓴다).
- 첫 페이지 집합은 저장소 최상위 디렉터리와 그 아래 코드 디렉터리(`apps/agent`, `apps/web-desktop`, `cli/llm-agent` 등) 10~20개로 시작한다. `feature:` 페이지는 사람이 만든다. (2026-10-18: 이제 기본 집합이 설계 문서마다 기능 페이지 `feature-<문서 이름>`도 만든다. `code-wiki-coverage.md`의 A.)
- 토론이 끝날 때 위키를 자동으로 고치지 않는다. 대시보드에서 "stale 페이지 다시 쓰기"를 누르거나 CLI `--stale`로 돌린다. 정해 둔 시각의 자동 실행은 두지 않는다(결정 3).

### 기록

- 스레드 `context_snapshot`에 `index: {"sha", "version", "files", "symbols"}`와 `wiki: {"shown": [id], "states": {id: state}}`를 더한다.
- `debate_started.data.context`에 `map_bytes`, `wiki_pages`, `wiki_stale`를 더한다.
- transcript `retrievals[]`는 그대로이며 새 op도 같은 항목 모양이다.

### 구현 메모 (P3)

구현: `cli/llm-agent/code_wiki.py`(front matter, 저장, 낡음 판정, 생성 영역, 위키 op의 본문), `wiki_build.py`(쓰기 작업), `wiki_ops.py`(P4가 호출할 JSON 명령), `repo_tools.py`·`repo_mcp.py`·`llm_runner.py`·`debate_engine.py`·`guardian.py`의 연결. 설계와 다르게 구현했거나 설계에 없던 결정이다.

**저장과 형식**

- 위치는 설계대로 `<GUARDIAN_DATA_DIR>/code-wiki/<project>/pages/<id>.md`(프로젝트 id는 코드 인덱스 캐시와 같은 규칙으로 정리). 페이지 id `^[a-z0-9][a-z0-9-]{0,63}$`, 본문 256KB 상한(UTF-8 바이트), 제목 한 줄 200자, scope 20개, basis 400개. 쓰기는 같은 디렉터리의 임시 파일에 쓰고 `fsync` 뒤 `os.replace`(원자적), 파일 0600, 우리가 만드는 디렉터리(`code-wiki`, `<project>`, `pages`) 0700. 읽을 때는 `O_NOFOLLOW`로 열고(심볼릭 링크 거부) 파일 크기 상한과 "front matter의 id = 파일 이름"을 검사한다. 읽을 수 없는 파일은 목록의 `errors`에 `{id, error}`로 나오고 나머지는 계속 쓴다.
- **front matter는 PyYAML 없이 직접 읽고 쓰는 제한된 YAML**이다(`code_wiki.py` 머리 주석이 정의). 줄 하나는 `key: 값`이고 값은 한 줄의 JSON 값(문자열, 숫자, 목록, 객체)이거나 `A-Za-z0-9_.@/+:-`로만 된 맨 토큰(숫자·`true`·`false`·`null`로 읽히면 JSON, 아니면 문자열)이다. `basis:`만 쓰고 이어지는 들여쓴 줄 `  "경로": "blob id"`는 문자열 쌍의 블록 매핑이다. 줄 전체 `#` 주석과 빈 줄은 무시하고, 그 밖의 것(작은따옴표 문자열, 인라인 주석, 중복 키, 닫는 `---` 없음)은 `FrontMatterError`다. 쓰기는 모든 문자열을 JSON 따옴표로 내보내 값에 줄바꿈이 들어가지 않고, 줄은 `"\n"`으로만 나눈다(`\u2028` 같은 문자에 안전). 본문에 `---` 줄이 있어도 첫 닫는 `---`까지만 front matter다.
- 설계의 필드에 `prompt_version`(쓰기 프롬프트 버전 `wiki_build.WIKI_PROMPT_VERSION`, 사람 페이지는 빈 문자열)과 `unverified`(쓸 때 되묻기 뒤에도 남은 미검증 인용 목록)를 더했다. `author`는 `{kind: llm, provider, job}`(`job`은 쓰기 작업의 스레드 id `wiki_<시각>_<페이지 id>`) 또는 `{kind: human, user}`.

**낡음 판정** (`code_wiki.page_state(page, tools)`, 저장하지 않고 sha마다 계산)

- `stale`의 "새 파일"은 **`written_sha`의 트리에 없던** scope 안의 읽을 수 있는 파일 중 `basis`에 없는 것이다. `basis`는 "읽은 파일"이라 scope의 파일 전부가 들어 있지 않으므로(에이전트는 일부만 읽는다), 단순히 "basis에 없는 파일"로 보면 처음부터 낡은 페이지가 된다. 그래서 `git ls-tree`로 `written_sha`의 트리를 보고 그때 없던 파일만 센다. 그 커밋을 쓸 수 없으면(gc 등) 새 파일 검사는 건너뛰고 `notes`에 이유를 적는다(`written_sha == 현재 sha`면 새 파일은 없다).
- `basis` 파일이 읽을 수 없게 되거나 없어지면 `removed`, blob id가 다르면 `changed`이며 둘 다 `stale`이다.
- `broken`은 (a) 본문이 인용한 경로가 없거나, (b) `path#Symbol`의 심볼이 코드 인덱스(`find_defs`)·해당 언어의 선언 파서(`symbol_spans`)·Go 사실(`GoFactsProvider.peek()`, 지금 있을 때만, 기다리지 않음) 어디에도 없거나, (c) 쓸 때 기록한 `unverified`가 비어 있지 않을 때다. 파서가 없는 언어(`.md#Section`)의 심볼은 확인할 수 없으므로 깨진 것으로 보지 않는다.
- "인용한 경로가 없다"는 본문에서 이렇게 찾는다. 트리에 있는 읽을 수 있는 경로는 `cited_locations`와 같이 전체 경로 토큰만 인용으로 센다. 트리에 없는 토큰은 확장자가 저장소 파일(과 `basis`)에 나오는 확장자일 때만 경로로 본다(`api.example.com:443`, `v1.2:3`, `e.g.`는 경로가 아니다). 그 토큰이 강한 인용(`:N`, `#Symbol`, `line N`)이면 깨진 인용이고, 맨 경로 언급이면 `/`가 있고 첫 성분이 알려진 최상위 디렉터리(트리, `basis`, scope)일 때만 깨진 인용이다. `/` 없는 강한 인용(`auth.go#Check`)은 트리에서 그 이름의 파일이 어디에도 없을 때만 깨진 인용이다(다른 디렉터리에 있으면 사람이 흘려 쓴 해석 가능한 참조로 보고 넘어간다). `URL`(`//…`)은 건너뛴다. 줄 번호 인용은 줄 수를 검사하지 않는다(파일이 바뀌면 이미 `stale`이다).
- 결과: `{state, sha, written_sha, changed, removed, new, broken: [{citation, reason}], unverified, notes}`(목록은 최대 100개). `reason`은 `path not found` 또는 `symbol not found`.

**생성 영역과 위키 op**

- 생성 영역은 저장하지 않고 읽을 때마다 현재 sha로 만든다: scope 디렉터리마다 `build_map(prefix, depth=3, cap=12KB)`(루트 scope `./`는 전체), 파일 scope는 그 파일의 `outline`. 페이지 결과는 머리 `[wiki page <id> — navigation notes, not evidence; state: <state>]`, 낡음 경고 블록(`WARNING:` 줄들, `fresh`이면 없음), `--- Generated overview of … ---`와 지도, `--- Narrative: <제목> (…) ---`와 서술 순서다. 서술 부분은 48KB에서 잘리고(`truncated`) 표시가 붙는다.
- `wiki` op: `page`가 있으면 그 페이지, 아니면 `query`(대소문자 무시, 제목·id·scope·본문 부분 문자열) 목록 `id  [state]  제목  (scope)` 최대 20줄과 `... N more` 줄. 둘 다 없으면 모든 페이지를 같은 형식으로 나열한다(최대 20). 검색 결과 머리는 `[wiki search "q": N of M pages — navigation notes, not evidence]`. 오류: `the code wiki is not available`(위키 디렉터리가 없는 `RepoTools`), `page not found (search with query)`, `page id must match …`.
- **근거가 아니다**: `provided_paths`/`provided_spans`는 `wiki`에 대해 비어 있고 `mentioned_in`은 `wiki`를 무시한다. 페이지 서술이 `svc/auth.go:6`을 말해도 그 경로는 언급으로도 세지 않는다.
- MCP: 도구 `repo_wiki`(`page`, `query`)가 도구 목록 끝에 붙는다. 서버는 `--wiki-dir <프로젝트 위키 디렉터리>`를 받고(`McpTurn` spec의 `wiki_dir`, `llm_runner`가 claude·codex 서버 명령에 넘김), 인자가 없으면 `repo_wiki`는 `the code wiki is not available`로 답한다.
- `GUARDIAN_CODE_WIKI=0`(`false`/`off`/`no`)이면 엔진이 `RepoTools`에 위키 디렉터리를 주지 않는다: 프롬프트에 위키 절이 없고, `wiki` op와 `repo_wiki`는 "not available"이며, 이벤트의 `wiki_pages`·`wiki_stale`은 0이다(P5 평가의 비교 구성용 스위치).
- **2026-10-18부터 기본 끔 (평가 2차 뒤 사용자 결정).** 45번의 실행에서 에이전트가 목록에 있는 페이지를 한 번도 열지 않았고, 위키는 정답과 비용 어느 쪽도 바꾸지 않았다. 그래서 `GUARDIAN_CODE_WIKI`가 `1`/`true`/`on`/`yes`가 아니면 `wiki_enabled()`는 거짓이다. 위키는 이제 사람이 쓴다(대시보드 Wiki 탭, 파일 보기, 인용 칩). 작성기는 그대로다. 위키 페이지(디렉터리·그룹·기능 페이지)는 전체 저장소 감사 모드의 범위 단위와 점검 목록으로 쓸 계획이다.

**첫 프롬프트, 스냅샷, 이벤트**

- 위키 절은 지도(또는 파일 목록) 바로 뒤, `=== End of repository retrieval ===` 앞에 붙는다: `=== Code wiki (navigation notes) ===`, 안내문(위키는 길잡이이고 근거가 아니며 인용하지 말고 직접 읽은 코드를 인용하라, 위키가 코드와 다르면 코드가 맞다, `{"op": "wiki", "page": "<id>"}`·`mcp__repo__repo_wiki`로 열고 `query`로 찾는다), `fresh` 페이지 `id — 제목 (scope)` 줄(최대 40줄, 제목 120자·scope 160자로 자르고 한 줄로 합침), 40줄을 넘으면 `... N more fresh pages`, 낡은 페이지가 있으면 `N more pages are stale or broken: a wiki search lists them with their state; …`, `=== End of code wiki ===`. **`fresh` 페이지가 하나도 없으면 절 자체가 없고 프롬프트는 p2.6과 바이트 단위로 같다**(`test_code_wiki.WikiPromptTests`가 `_retrieval_intro`의 세 입력에 대해 p2.6 때 구한 sha256을 고정하고, 엔진 첫 프롬프트에 `wiki`라는 단어가 없음을 확인한다). 그래서 낡은 페이지만 있는 프로젝트의 에이전트는 `wiki` op의 존재를 프롬프트로 알지 못한다(MCP 도구 목록에는 보인다).
- `RETRIEVAL_PROMPT_VERSION = "2026-10-12.p3"`. 위키 상태 계산이 실패해도 토론은 계속된다(경고만 찍고 절 없음).
- 스레드 `context_snapshot.wiki = {"shown": [id], "states": {id: state}}`는 페이지가 하나라도 있을 때만 생긴다. `debate_started.data.context.wiki_pages`는 프로젝트의 전체 페이지 수, `wiki_stale`은 그 sha에서 `fresh`가 아닌 수(항상 있고 없으면 0). `docs/design/debate-agent-retrieval.md`의 이벤트 표와 "구현 메모 (P3)"에 반영했다.

**쓰기 작업** (`wiki_build.WikiBuilder`, 수동 실행만)

- 명령: `python3 cli/llm-agent/guardian.py --code-wiki-build --project P [--page ID ... | --stale | --all] [--sha S] [--team T] [--force] [--language L] [--json] [--events-file F] [--result-file F]`. 설계의 `code-wiki build` 하위 명령 대신 기존 플래그 방식(`--debate`, `--projects`)을 따랐다. `--page`는 반복 가능하고 `--page`·`--stale`·`--all`은 함께 쓸 수 없다. 선택이 없으면 **프로젝트에 페이지가 하나도 없을 때만** 기본 집합을 쓴다(페이지가 있으면 오류 2: `pages exist already: pass --page ID, --stale or --all`).
  - `--page ID`: 있는 페이지는 다시 쓰고, 없는 id는 기본 집합 후보의 id일 때만 새로 만든다(그 밖은 종료 코드 3). `--stale`: 상태가 `fresh`가 아닌 페이지, 그리고 사람이 쓰지 않은 페이지 중 `prompt_version`이 `WIKI_PROMPT_VERSION`과 다른 것(구조 검토의 "키 = basis blob + prompt_version"). `--all`: 있는 모든 페이지. 사람 페이지는 `--force`가 없으면 LLM을 부르기 전에 건너뛰고 이유에 지금 상태를 적는다(상태는 저장하지 않으므로 "stale 표시만"은 이 보고와 `--code-wiki-list`가 대신한다).
  - 기본 집합(`default_page_set`): 최상위 디렉터리마다 코드 파일(`code_index.language`가 아는 확장자와 `.sh` `.rs`; 점으로 시작하는 최상위 디렉터리, 테스트·미리보기 경로는 제외)이 있는 하위 디렉터리 한 단계(`apps/agent/`, `cli/llm-agent/`, `packages/guardian-ui/` …)마다 페이지 하나, 최상위 디렉터리에 직접 코드 파일이 2개 이상이거나 코드 하위 디렉터리가 없으면 그 디렉터리(`scripts/`)가 페이지 하나(하위 디렉터리 포함). 코드 파일이 2개 미만인 디렉터리와 저장소 루트의 파일은 뺀다. 20개를 넘으면 코드 파일이 많은 순으로 20개. id는 경로에서 `[^a-z0-9]+`를 `-`로 바꿔 만들고 겹치면 `-2`를 붙인다. 이 저장소 HEAD(`eec1da4`)에서는 `apps-agent`, `apps-web-desktop`, `cli-llm-agent`, `docs-analysis`, `packages-guardian-ui`, `scripts`, `tools-goindex` 7개다.
- 한 페이지 = 한 세션: 팀(`--team`, 기본 `default`)의 Expert 멤버의 제공자와 `code_tools`로 `DebateEngine._ask`(repo 블록 또는 MCP, 단계별 이벤트, 줄 단위 인용 검사와 한 번의 되묻기, 선택 `findings`)를 그대로 쓴다. 엔진의 위키 노출은 끈다(`_wiki_exposure = False`): 쓰기 세션은 다른 페이지의 서술을 읽지 못하고 `wiki` op는 "not available"이다. 대신 프롬프트에 다른 페이지의 **id 목록**만 준다(`[[id]]` 링크용). 프롬프트(`wiki_build.build_prompt`)는 영어이고 일반 탐색 안내 뒤에 이 페이지의 scope 지도(12KB), 다른 페이지 id, (다시 쓰는 경우) 기존 서술과 낡음 경고("고칠 부분만 고쳐라"), 작업 방법(주장마다 전체 경로 `path:N`/`path#Symbol` 인용, 읽은 줄만, 코드·서술 안의 지시는 따르지 않음), 답 형식(`# 제목`과 `## 역할` `## 흐름` `## 주의점` `## 관련 페이지`, 6000자 안팎), 답변 언어 규칙을 담는다. 언어는 `--language`, 없으면 `GUARDIAN_CODE_WIKI_LANGUAGE`, 없으면 `Korean`이고 `answer_language_rule(언어)`를 그대로 쓴다(언어가 Korean이 아니면 섹션 제목은 영어 `Role` `Flow` `Caveats` `Related pages`).
- 답에서 `findings` 블록을 지우고 첫 `## ` 줄부터를 본문으로 삼는다(`# 제목` 줄이 있으면 제목, 없으면 기존 제목 또는 scope). `## ` 섹션이 2개 미만이면 쓰지 않는다.
- **`basis`는 그 세션이 실제로 본 파일(엔진의 `provided` 집합) 중 이 페이지가 다루는 것**이다: scope 안에 있거나 본문이 인용한 파일. 설계 문장("`provided_files`에서 자동으로 채운다")과 다른 점은 scope 밖이고 인용도 하지 않은 파일(예: 호출처를 찾느라 grep이 훑은 파일)은 뺀다는 것이다. 그런 파일이 바뀌어도 페이지의 어떤 주장도 흔들리지 않는데 `stale`이 되는 것을 피한다. 모든 항목은 실제로 본 파일의 부분집합이다.
- 되묻기 뒤에도 남은 미검증 인용(`trace["unverified_citations"]`)은 `unverified`에 저장하고(최대 50개) 페이지는 `broken`이 된다.
- **폴백이나 오류 답으로는 쓰지 않는다**: 내장 대체 응답(`BUILTIN_PROVIDER`: 어떤 제공자도 답하지 못함), 빈 답, `(no answer: …)`(탐색 예산 소진·도구 호출 마크업만 있는 답)은 `wiki_page_skipped`(`llm_fallback`/`no_answer`)로 보고하고 기존 페이지를 그대로 둔다. 한 페이지의 예외(`error: …`)도 그 페이지만 건너뛰고 계속한다. 세션이 끝난 뒤 쓰기 직전에 페이지를 다시 읽어, 그 사이 사람이 저장한 페이지라면 덮어쓰지 않는다(`--force` 제외).
- **동시성**: 프로젝트마다 `<wiki 디렉터리>/build.lock`에 `flock(LOCK_EX|LOCK_NB)`를 잡는다(빌드 전체). 둘째 빌드는 즉시 종료 코드 4(`another code wiki build is running for this project`)다. 프로세스가 죽으면 커널이 잠금을 풀므로 정리할 것이 없다. 페이지는 한 번에 하나씩 쓰므로 한 빌드 안에서 LLM 세션은 동시에 하나다.
- 이벤트(`--events-file`, 토론과 같은 `EventWriter`): `wiki_build_started`, 페이지마다 `wiki_page_written {page, state, basis_files, citations, unverified}` 또는 `wiki_page_skipped {page, reason}`, `wiki_build_finished {written, skipped, failed}`. 세션 중에는 `agent_started`, `retrieval_*`, `citation_check`, `evidence_check`, `agent_finished`가 토론과 같은 모양으로 나온다(스레드 id는 페이지마다 `wiki_<시각>_<페이지 id>`, 세션은 `--sessions`에 남아 `--attach`로 열 수 있다). 시작 전에 실패하는 경우(프로젝트·팀·sha 오류, 선택 오류, 잠금)에는 이벤트가 없다.
- 결과(`--result-file`, `--json`): `{project, sha, team, language, force, pages: [id], written: [{page, state, basis_files, citations, unverified}], skipped: [{page, reason}], failed}`(`--json`은 앞에 `"ok": true`). 종료 코드: 0 실행함, 1 하나도 쓰지 못했고 실패(`failed` > 0)가 있음, 2 잘못된 입력(프로젝트·팀·sha·선택·`GUARDIAN_RETRIEVAL=0`), 3 모르는 페이지 id, 4 이미 빌드 중. `--json`이면 시작 전 오류도 stdout에 `{"ok": false, "error": …}`를 낸다.
- **쓰기 세션의 역할과 첫 프롬프트** (허브 실패 후 보정, `WIKI_PROMPT_VERSION` 2026-10-12.wiki2): `llm_runner._build_prompt`는 모든 세션 호출 앞에 "expert Linux System & SSH Security Engineer" 역할 줄을 붙이고, 이어받는 턴마다 다시 붙였다. 쓰기 세션은 이 줄을 `wiki_build.WRITER_SYSTEM`(코드베이스 문서 작성자, 영어)으로 바꾸고(`run_session_prompt(system=...)`), 이어받는 턴은 머리말 없이 보낸다. 팀 Expert의 persona 프롬프트는 쓰지 않고 제공자 순서·`code_tools`만 쓴다. 첫 프롬프트의 규약 안내는 저장소 전체 지도 대신 `root_summary`를 쓴다(페이지 범위 지도는 과제에 따로 들어간다): 허브 입력 50,624바이트가 이 저장소의 `cli-llm-agent`에서 약 19 KB가 된다. `--code-wiki-build ... --dry-run [--json]`은 LLM 호출 없이 페이지마다 첫 프롬프트의 구성(protocol, guide, scope_map, task, total 바이트)을 보여 준다.
- **세션 문맥 손실의 원인** (허브 확인, `WIKI_PROMPT_VERSION` 2026-10-12.wiki3): 첫 응답의 repo 블록 14개가 합쳐진 70개 요청의 결과가 722KB 메시지 하나로 돌아갔고, claude CLI가 세션을 자동 압축했다(세션 기록의 `compact_boundary`, "Conversation compacted"). 압축 뒤 모델은 과제와 읽은 내용을 잃었다. 사용자 결정(2026-10-12): 위키 쓰기에도 단계 상한은 두지 않는다. 대신 `WRITER_SYSTEM`에 "작은 묶음으로 읽어라, 큰 파일은 개요 먼저" 안내를 넣고, 토론과 공통인 `retrieval_warning` 기준을 2 MiB에서 256 KiB로 낮춰 이런 단계가 이벤트로 보이게 했다.
  - **바뀜(2026-10-13, jev 결정):** 같은 사고가 기능 페이지 `feature-debate-code-index-wiki`에서 다시 났다(첫 단계 3.1 MB, 세션 압축 뒤 과제를 잃고
    섹션 없는 답으로 건너뜀). 그래서 위키 쓰기 세션에만 **한 단계 제공량 상한**을 둔다: `wiki_build.writer_limits()`가 빌드 동안
    `GUARDIAN_RETRIEVAL_STEP_BYTES`를 `GUARDIAN_CODE_WIKI_STEP_BYTES`(기본 512 KiB, 0이면 끔; 명시한 더 작은 값은 그대로)로 두고 끝나면 되돌린다.
    넘친 요청은 기존 메커니즘대로 `deferred`로 돌려주어 다음 단계에 다시 보내게 한다(블록·MCP 모두). 전체 읽기량은 여전히 무제한이고,
    토론 세션에는 적용하지 않는다. 상한 도입과 값(512 KiB, 확신도 0.75)은 사용자 지시에 따라 jev로 정했다.
  - **바뀜(2026-10-16, `WIKI_PROMPT_VERSION` 2026-10-16.wiki6):** 근본 원인은 역할과 과제가 첫 사용자 메시지에만 있다는 점이었다. claude 세션에서는 `WRITER_SYSTEM`, 탐색 규약, 루트 요약, 언어 규칙이 이제 시스템 프롬프트(`--append-system-prompt`, 매 호출에 같은 텍스트)에 있고 압축 뒤에도 남는다. 첫 사용자 메시지는 페이지 과제(`build_prompt`)뿐이다. 과제 자체는 압축되면 요약되지만, 페이지 쓰기에서는 역할, 규약, 언어, 인용 규칙이 남으므로 모델이 다시 읽고 답할 수 있다. 다른 제공자의 첫 메시지는 그대로다. 자세한 내용은 `debate-agent-retrieval.md`의 "시스템 프롬프트와 압축".

**읽기·편집 명령** (`wiki_ops.py`, P4가 `guardian.py`로 호출). 모두 JSON을 stdout에 내고(`--json`은 받기만 한다) `config_ops`와 같은 종료 코드(0 성공, 2 잘못된 입력·열 수 없는 프로젝트/sha, 3 모르는 페이지, 4 빌드 충돌)를 쓴다. 상태는 `--sha S`(기본 프로젝트 작업 클론의 HEAD) 기준이다. 오류 본문은 `{"ok": false, "error": "…"}`.

| 명령 | 입력 | 출력 |
|---|---|---|
| `--code-wiki-list --project P [--sha S]` | | `{"ok", "project", "sha", "pages": [{"id", "title", "scope": [..], "author": {..}, "updated", "state"}], "errors": [{"id", "error"}]}` |
| `--code-wiki-show ID --project P [--sha S]` | | `{"ok", "project", "sha", "page": {"id", "title", "scope", "author", "updated", "written_sha", "prompt_version", "basis": {경로: blob}, "unverified": [..]}, "narrative": "<본문>", "state", "staleness": {"state", "sha", "written_sha", "changed": [..], "removed": [..], "new": [..], "broken": [{"citation", "reason"}], "unverified": [..], "notes": [..]}, "generated": "<scope 지도 텍스트>"}` (`--json` 없이 부르면 사람이 읽는 텍스트) |
| `--code-wiki-save --project P [--sha S] [--actor U]` | stdin JSON `{"id", "title", "scope": [..], "body"}` (다른 키는 거부) | `{"ok", "project", "sha", "page": {…show와 같은 메타}, "state", "staleness": {…}}`. 엔진이 front matter를 다시 쓴다: `author {kind: human, user: --actor 또는 OS 사용자}`, `written_sha` = 검사한 sha, `basis` = 본문이 강하게 인용한 읽을 수 있는 파일의 현재 blob, `prompt_version` "", `unverified` []. scope 항목은 읽을 수 있는 파일 또는 디렉터리여야 하고(디렉터리는 끝에 `/`가 붙어 저장, `.`·`./`는 저장소 전체) 입력은 약 320KB까지, 본문은 256KB까지 |
| `--code-wiki-delete ID --project P` | | `{"ok", "project", "deleted": ID}`, 없으면 종료 코드 3 |
| `--code-wiki-build …` | 위 "쓰기 작업" | 위 결과 JSON |

(2026-10-18: 목록 항목과 `page` 메타에 `kind`가, `page` 메타에 `docs`가 더해졌고, `--code-wiki-build`는 `--kind`, `--regroup`, `--dry-run`을 받는다.
파일 보기 명령 `--code-file-list`, `--code-file-show`, `--code-file-read`는 `file_view.py`에 있다. `code-wiki-coverage.md`.)

**P4가 쓰기 작업을 띄울 때**: 쓰기 작업은 토론 작업이 아니다. Go 허브의 토론 동시 실행 한도(`MaxRunning`)를 쓰지 말고 `code_wiki` 작업 종류에 별도의 슬롯(전체 1개)을 둔다. 프로젝트별 중복 방지는 Python의 `build.lock`이 이미 하므로(종료 코드 4 → HTTP 409) Go는 슬롯만 관리하면 된다. 토론과 같이 job 디렉터리를 만들고 `--events-file <job>/events.jsonl --result-file <job>/result.json --json`으로 실행하되, 사람이 대시보드에서 저장한 페이지(`PUT`)는 같은 파일을 원자적으로 교체하므로 빌드와 겹쳐도 깨지지 않는다(빌드는 쓰기 직전에 사람 페이지를 다시 확인한다). 이벤트 소비 쪽(Agents 모니터)은 모르는 종류를 건너뛰므로 `wiki_*`가 와도 괜찮지만, `debate_started`가 없는 작업이라 모니터가 작업 머리에 쓸 값(프로젝트, 페이지 목록)은 `wiki_build_started`에서 읽어야 한다.

**알려진 한계와 미해결**

- `fresh`는 "basis의 blob이 그대로이고 인용한 경로·심볼이 있다"일 뿐 서술이 옳다는 뜻이 아니다. basis에 없는 파일의 변화는 보지 않고(scope 안의 새 파일만 `stale`로 센다), 줄 번호 인용이 가리키는 줄 수는 검사하지 않는다(그 파일이 바뀌었다면 이미 `stale`이다).
- `path#Symbol` 확인은 레이아웃 파서의 한계(P1 구현 메모)를 그대로 가진다. 파서가 놓친 선언(TS 클래스 멤버 등)은 `symbol not found`로 `broken`이 될 수 있다.
- 사람이 `fresh`인 LLM 페이지를 고치지 않고 `--page`로 다시 쓰면 항상 LLM을 부른다(캐시 적중 생략은 `--stale`에만 있다).
- 쓰기 세션의 LLM 비용·시간은 토론과 같이 제한이 없다(`GUARDIAN_RETRIEVAL_*`가 설정된 경우만 그 예산). 20페이지 기본 집합을 한 번에 돌리면 세션 20개이므로 첫 실행은 `--page`로 몇 개씩 시험하는 편이 좋다.
- 첫 배포 때 허브에서 `--code-wiki-build --page cli-llm-agent`로 실제 제공자 응답 형식(섹션 제목 준수, 인용 검사 통과율)을 확인해야 한다. 이 단계의 테스트는 가짜 러너만 쓴다.

**테스트**

- `test_code_wiki.py`(약 80개): front matter(왕복, 맨 토큰·주석, 잘못된 입력, 필드 검증), 저장(권한, 원자적 교체와 실패 시 원본 유지, id 탈출 시도, 심볼릭 링크·크기·id 불일치, 잠금), 낡음 판정(fresh, blob 변경, 삭제, 새 파일과 기존·범위 밖 파일, 쓴 커밋이 없는 경우, 이동한 심볼, 미검증 인용, 파이썬 메서드, 파서 없는 언어, 경로 인용 규칙, Go 사실), 위키 op(머리, 생성 영역, 경고 블록, 검색, 상한, 오류, 파일·범위·언급 없음, MCP 도구·프로세스·명령 인자), 첫 프롬프트(위키 없음 = p2.6과 같은 바이트, 낡은 페이지만 있을 때, 목록과 안내문, 40줄 상한, 스위치, 위키 오류 격리), **미끼 검증 2**(아래), 쓰기 작업(성공, 이벤트, 프롬프트 내용, 위키 노출 끔, 남은 미검증 인용, 되묻기로 해결, basis 규칙, 폴백·형식 오류·빈 답, 사람 페이지, 세션 중 사람 저장, 낡은 페이지 재작성 프롬프트, 잠금, 사라진 scope, 오류 입력, 옛 커밋), CLI(저장·목록·보기·삭제, sha별 상태, 잘못된 입력 종료 코드, 빌드와 종료 코드). `test_db_backup.py`에 `code-wiki/`가 타르볼에 남는 테스트를 더했다.
- **미끼 검증 2(결정론적)**: 페이지가 "인증은 `svc/auth.go#Check`(`svc/auth.go:6`)에서 한다"고 말하는데 코드에서 `Check`가 `svc/guard.go`로 옮겨진 저장소를 만든다. (1) 그 페이지는 `broken`(`svc/auth.go#Check (symbol not found)`)이고 첫 프롬프트에 나열되지 않으며 `wiki_pages`·`wiki_stale`이 1·1이다. (2) 에이전트가 `wiki` op로 페이지를 열면 결과가 `state: broken`과 깨진 인용 목록을 보여 준다. (3) 위키만 보고 `svc/auth.go:6`을 인용한 최종 답은 `citation_check.unverified == ["svc/auth.go"]`로 걸려 한 번 되물어지고, transcript의 위키 항목 `paths`는 `[]`이며 스레드 스냅샷에 `provided_files`/`provided_spans`가 없다. (4) 같은 답이라도 `svc/auth.go`를 `read`한 뒤에는 통과한다.
- 기대값을 의도적으로 바꾼 기존 테스트: `test_evidence.py`·`test_go_facts.py`(프롬프트 버전 `2026-10-12.p3`), `test_llm_runner.py`·`test_repo_mcp.py`(도구 목록 끝에 `repo_wiki`).
- 짧은 인용(P2.7): 작성기 답이 `guardian.py:755`, `(line 510)`처럼 쓴 인용도 토론과 같은 검사로 파일 하나에 풀어 P2.5 줄 규칙으로 검증한다(규칙은 `debate-agent-retrieval.md`의 "짧은 인용 (P2.7)"). 해결된 파일은 `wiki_page_written.citations`에 들어가고, 풀리지 않은 것은 페이지 `unverified`에 문자열로 남는다. 작성기 프롬프트는 전체 경로만 허용하도록 바뀌었고 `WIKI_PROMPT_VERSION`은 `2026-10-13.wiki4`다. 낡음 판정의 `broken_citations`는 전체 경로 인용만 다시 확인한다.

## Go API와 대시보드

| 메서드·경로 | 내용 |
|---|---|
| `GET /api/projects/{id}/wiki` | 페이지 목록(`id, title, scope, author, updated, state`). `state`는 프로젝트 작업 클론의 HEAD 기준 |
| `GET /api/projects/{id}/wiki/{page}` | 페이지 하나(서술, 생성 영역, 낡음 상세) |
| `PUT /api/projects/{id}/wiki/{page}` | 사람 편집 저장. 본문 256KB 상한, front matter는 엔진이 다시 쓴다 |
| `POST /api/projects/{id}/wiki/build` | `code_wiki` 작업 시작(`{"pages": [...]}` 또는 `{"stale": true}`) |

- 인증은 `main.go`의 전역 `withSecurity(withAuth(mux))`이다(`h.guard`는 CLI가 있는지만 본다). 페이지 id는 `^[a-z0-9][a-z0-9-]{0,63}$`로 제한한다(경로 밖으로 나가지 않게 한다).
- 대시보드: 프로젝트 화면에 `Wiki` 탭을 두고, 목록(상태 칩), 페이지 보기(서술과 생성 영역 구분), 편집, "stale 다시 쓰기"를 둔다. 작성 작업은 Agents 모니터에 토론처럼 나타난다. (2026-10-18: 탭은 Debates 페이지의 구역 전환에 있다. 아래 "구현 메모 (P4)".)
- 파일 보기 경로 `GET /api/projects/{id}/files`, `.../files/show?path=`, `.../files/lines?path=&start=&end=`는 `apps/agent/file_handler.go`에 있다(`code-wiki-coverage.md`의 C).

### 구현 메모 (P4)

구현: Go `apps/agent/wiki_handler.go`(경로, 검증, 쓰기 작업 시작), `debate_handler.go`(작업 `kind`, 동시 실행 계산, 종료 처리), 대시보드 `apps/web-desktop/src/pages/wiki/`(`WikiTab`, `WikiPageView`, `WikiEditDialog`), `lib/wiki.ts`, `lib/agentMonitor.ts`·`pages/agents/*`(작업 모니터), `api.ts`·`types.ts`. 설계 표와 다르게 구현했거나 설계에 없던 결정이다.

**경로** (모두 `h.guard` 뒤, 프로젝트 id는 기존 `idPattern`, 페이지 id는 `^[a-z0-9][a-z0-9-]{0,63}$`, `sha`는 `contextRefRe`이고 `-`로 시작할 수 없다. 모두 `--json`과 단일 `--flag=값` argv로 호출. 2026-10-18: 인증은 전역 `withAuth`가 하고 `h.guard`는 CLI가 있는지만 본다)

| 경로 | CLI | 비고 |
|---|---|---|
| `GET /api/projects/{id}/wiki[?sha=]` | `--code-wiki-list --project=ID [--sha=S]` | CLI 시간 제한 120초(첫 호출은 코드 인덱스를 만든다) |
| `GET /api/projects/{id}/wiki/{page}[?sha=]` | `--code-wiki-show=PAGE --project=ID` | 응답은 CLI JSON 그대로(`narrative`, `generated`, `staleness`) |
| `PUT /api/projects/{id}/wiki/{page}` | `--code-wiki-save --project=ID --actor=<actorFor>` | 본문 `{title, scope, body[, id]}`. 요청 상한 320KB(CLI 입력 상한, 넘으면 413), 본문 텍스트는 256KB(UTF-8 바이트, 넘으면 413). `id`가 있으면 경로 값과 같아야 하고(아니면 400) 없으면 엔진이 채운다. 새 id면 페이지가 만들어진다 |
| `DELETE /api/projects/{id}/wiki/{page}` | `--code-wiki-delete=PAGE --project=ID` | |
| `POST /api/projects/{id}/wiki/build` | `--code-wiki-build --project=ID --json [--page=P ...\|--stale\|--all] [--force]` | 아래 "쓰기 작업" |

- 종료 코드 매핑은 기존 `respondConfig`와 같다: 0 → 200(CLI JSON), 2 → 400, 3 → 404, 4 → 409, 그 밖 → 500. CLI가 없으면 503(`guard`). 행위자(`--actor`)는 다른 결정 경로처럼 `actorFor(RemoteAddr)`(`dashboard@<host>`)다. 허브에는 사용자 세션 이름이 없어 "세션 사용자" 대신 이것을 쓴다.
- **`GUARDIAN_UI_ACTIONS=off`는 위키 경로에 적용하지 않는다.** 그 스위치는 실행·거절·닫기·게시·작업 공간 초기화 같은 결정 경로를 막는 것이고, 페르소나·팀·프로젝트 저장과 토론 시작은 막지 않는다. 위키 편집과 쓰기 작업은 후자와 같은 종류라고 보았다(테스트로 고정).
- 경로 충돌: `POST …/wiki/build`는 `PUT/GET/DELETE …/wiki/{page}`와 메서드가 달라 겹치지 않는다. 다만 `build`라는 id의 페이지도 `GET/PUT/DELETE`로 다룰 수 있다.

**쓰기 작업** (`code_wiki`)

- 요청 `{"pages": [id…]}` | `{"stale": true}` | `{"all": true}`와 선택 `"force"`. 둘 이상을 함께 주면 400이고, 아무것도 없거나 본문이 비어 있으면 CLI의 기본 페이지 집합이다(페이지가 없는 프로젝트만 허용, 있으면 CLI가 종료 코드 2 → 400). 모르는 필드는 400. `pages`는 50개까지. (2026-10-18: 선택 필드 `kind`(`dir`|`overview`|`group`|`feature`)가 더해져 `--kind`로 넘어간다. `pages`와 같이 쓸 수 없다.)
- 토론과 같은 `<GUARDIAN_DATA_DIR>/debate-jobs/<dbt_…>/` 레이아웃(`meta.json`, `output.log`, `events.jsonl`, `result.json`)과 `launch`를 쓴다. `meta.json`에 `kind: "code_wiki"`, `project`, `task`(`code wiki build: stale pages` 같은 영어 설명)가 있고 `team`·`thread_id`·`rounds`는 비어 있다. 그래서 `GET /api/debates/{id}`, `…/events`(폴링·follow), 목록, 취소가 그대로 동작한다.
- **자체 슬롯**: 실행 중인 `code_wiki` 작업은 `MaxRunning`(토론 한도)에 세지 않고, 토론도 쓰기 작업의 영향을 받지 않는다. 슬롯은 전체 1개이고 `GUARDIAN_MAX_WIKI_BUILDS`로 바꾼다. 같은 프로젝트에 이미 실행 중인 쓰기 작업이 있으면 409, 슬롯이 다 찼으면 429(토론 한도와 같은 코드)다. 프로젝트별 중복의 최종 방어는 CLI의 `build.lock`이다.
- **CLI의 즉시 거절을 동기로 돌려준다**: 작업은 비동기이지만 종료 코드 2·3·4(잘못된 입력, 모르는 페이지, 이미 빌드 중)는 시작 직후 나온다. 경로는 프로세스가 `WikiStartGrace`(기본 3초) 안에 그 코드로 끝나는지 기다렸다가 400·404·409와 CLI의 `{"ok": false, "error"}` 문구로 답하고 그 작업 디렉터리를 지운다(실패한 작업이 목록에 남지 않는다). 그 시간 안에 끝나지 않으면 202와 작업 요약이다. 파이썬 기동이 느려 늦게 거절되면 작업은 `failed`(`code wiki build exited with code 4: …`)로 목록에 남는다.
- 같은 제한 시간(`GUARDIAN_DEBATE_TIMEOUT`, 기본 1시간)과 같은 취소(`DELETE /api/debates/{id}` → 프로세스 그룹 SIGKILL, 상태 `cancelled`)를 쓴다. 이미 쓴 페이지는 그대로 남는다. 에이전트 재시작 때는 `interrupted`가 된다(오류 문구는 "agent restarted while the code wiki build was running").
- **`GET /api/debates` 동작**: 쓰기 작업을 같은 목록에 `kind: "code_wiki"`로 넣는다. 에이전트 모니터가 곧 토론 콘솔의 목록·상세(`#/debates`)라서 목록에서 빼면 모니터에서도 사라지기 때문이다. 토론 콘솔은 칩 "Code wiki build"를 붙여 보여 주고, 개요 화면의 "Recent debates"에서는 뺀다. 목록 상한(50)은 둘이 나눠 쓴다.

**대시보드 위치와 화면**

- 위치: Debates 페이지 맨 위의 구역 전환(`Debates | Wiki`)에서 Wiki. 경로는 `#/debates/wiki`(목록)와 `#/debates/wiki/<페이지 id>`(페이지 열기)이다. 프로젝트는 Debates와 같은 프로젝트 선택기를 쓴다. 별도의 "프로젝트 화면"이 없어 프로젝트 선택기가 있는 이곳에 두었다. 목록 갱신 5초 폴링은 `DebatesPage`가 구역과 관계없이 한다.
- Wiki 목록(`WikiTab`): 위쪽 카드에 프로젝트 선택, `Reload`, `New page`(손으로 새 페이지), `Rewrite stale (n)`, `Rewrite all`(확인 창), 페이지가 하나도 없으면 `Write the first pages`(기본 집합). 아래 왼쪽은 페이지 목록(제목, 상태 칩 FRESH/STALE/BROKEN, id, 작성자 `claude (LLM)`/`alice (person)`, 갱신 시각)과 확인한 커밋, 읽을 수 없는 페이지 오류. 오른쪽은 열린 페이지이고 없으면 "Select a page to read it."이다. 이 프로젝트의 쓰기 작업이 실행 중이면 build 버튼을 막고 모니터로 가는 링크를 보인다. 그 작업이 끝나면 목록을 다시 읽는다.
- 페이지 보기(`WikiPageView`): 머리(상태 칩, 제목, id, 작성자, 갱신, 쓴 커밋과 확인한 커밋, scope 칩)와 `Rewrite`·`Edit`·`Delete`. 낡음이면 경고 상자에 바뀐/사라진/새 파일, 깨진 인용(이유 포함), 미검증 인용, 메모를 나열한다. 그 아래 **서술**(작성자를 밝힌 캡션과 함께 전처리 텍스트)과 **생성 영역**("열 때 코드에서 만든 것, 저장하지 않는다", 접힌 상자)이 따로 있다. 대시보드에 Markdown 렌더러가 없어 서술은 소스 그대로 보여 준다(의존성을 더하지 않았다). (2026-10-18: 2026-10-13부터 서술은 Markdown으로 그리고 인용은 파일 보기로 가는 칩이다. `WikiNarrative.tsx`, `components/MarkdownDoc.tsx`.) `Delete`는 `ConfirmModal`, 사람이 쓴 페이지에 `Rewrite`하면 확인 뒤 `force: true`로 보낸다.
- 편집(`WikiEditDialog`): id(새 페이지만), 제목, scope(줄마다 하나), 본문 텍스트 영역과 크기 표시. 클라이언트에서 id 형식·제목·빈 본문·256KB를 먼저 검사하고 허브 오류는 대화상자 안에 보인다. 저장하면 작성자가 사람이 되고 basis는 본문의 강한 인용에서 채워진다(P3).
- 좁은 화면: Debates처럼 목록 또는 상세 하나만 보인다(`#/debates/wiki/<id>`에서 상세와 "← Back to pages").
- 에이전트 모니터(`#/debates/<작업 id>`): 목록 행에 "Code wiki build" 칩. 쓰기 작업은 끝난 뒤에도 결과 화면(토론 판정) 대신 모니터를 보인다. 머리에 칩과 "Open the wiki" 링크, 취소 대화상자는 쓰기 작업 문구, 결과 카드는 "Pages (n/m)"(페이지별 FRESH/STALE/BROKEN, SKIPPED, 아직 안 한 것). 레인은 쓰기 세션의 Expert 하나다. 타임라인은 `wiki_build_started`("Code wiki build started"), `wiki_page_written`("Page written", 상태 칩, 읽은 파일·인용 수, 미검증 인용 경고), `wiki_page_skipped`(경고, 기존 페이지 그대로), `wiki_build_finished`("N written")와, 토론에도 나오는 `retrieval_warning`(경고 상자), `evidence_check`(일치하면 한 줄, 불일치는 경고와 목록), `compiler_index`(상태 칩과 패키지·정의·참조 수), `compiler_index_warning`을 보인다. 작업 상태는 `wiki_build_finished`에서 정한다(쓴 페이지가 없고 실패가 있으면 `failed`, 아니면 `completed`; 건너뜀만 있으면 오류가 아니다).

**알려진 한계**

- 화면 문구는 기존 대시보드를 따라 영어다(대시보드에 한국어 문구가 없다). 라벨은 `lib/wiki.ts`의 `WIKI_JOB_LABEL`과 각 컴포넌트에 있다. (2026-10-18: 이제 대시보드 문구는 기본 한국어이고 영어를 고를 수 있다. 위키 문구는 `src/i18n/ko/wiki.ts`, `src/i18n/en/wiki.ts`에 있고 `WIKI_JOB_LABEL`은 `tr.wiki.jobLabel`이다. 그래서 위 영어 라벨은 영어 화면의 것이다. `dashboard-i18n.md`.)
- 목록과 페이지 보기는 기준 sha를 정하지 않는다(프로젝트 작업 클론의 HEAD). `sha` 질의는 API에만 있고 화면은 쓰지 않는다.
- 쓰기 작업의 거절을 기다리는 `WikiStartGrace` 동안 요청이 열려 있다(기본 3초). 그 사이에 같은 프로젝트로 온 둘째 요청은 슬롯 검사에서 바로 409다.
- 실제 허브에서 화면 확인(제공자 응답 형식, 긴 페이지의 모양)은 아직 하지 않았다. 테스트는 가짜 CLI와 모의 API만 쓴다.

## 단계

| 단계 | 범위 | 끝 조건 |
|---|---|---|
| P1 | `code_index.py`(세 언어 파서, blob 캐시), `RepoTools` blob id, `outline`·`path#Symbol`의 TS/TSX 지원, `defs`/`refs` op, MCP 도구, 프롬프트 안내 | 파서 단위 테스트(언어별 정형·비정형 예제), 기존 `test_code_select.py`·`test_repo_tools.py` 통과, 미끼 검증 1 |
| P2 | 저장소 지도, 첫 프롬프트 교체, `map` op, `GUARDIAN_REPO_MAP` | 지도 결정성 테스트(같은 sha → 같은 바이트), 32KB 축약 테스트, 평가 1차 |
| P2.1 | 견고성: `refs`/`defs`/지도의 색인 범위 표시(W2), 캐시 서명의 리터럴 가림과 `INDEX_VERSION` 2(W4), `refs` 일괄 읽기(W5), 비밀 필터를 전체 경로로(W4b), `retrieval_warning` 이벤트, 지도 순위(고유 파일 수, 노이즈 경로 제외), 캐시 정리와 백업 제외 | 끝 표시의 모든 경우 테스트, 캐시 `signature`에 리터럴 없음, `config/secrets/x.yaml` 읽기 불가, 같은 sha의 지도 결정성 |
| P2.5 | 근거: 줄 범위 기반 인용 검증(`GUARDIAN_CITATION_SLACK`, `provided_spans`, MCP 로그 `spans`), 선택 `findings` 블록 대조와 `evidence_check` 이벤트, provider별 findings 보고, 프롬프트 버전 `2026-10-12.p2.5` | 같은 이름 `Handle` 미끼를 잡는다, 보인 줄·슬랙·범위·심볼 규칙, P2.5 이전 스레드의 파일 단위 호환, 가짜 인용은 불일치, 판정자 형식이 findings 블록에 흔들리지 않음 |
| P2.6 | Go 컴파일러 보조 인덱스: `tools/goindex`, 임시 작업 트리와 정리, 사실 JSON, 타입 검사 `refs`/`defs`, `callers`/`callees` op와 MCP 도구, 지도 순위 반영 | 위 "검증 (P2.6)", 정리 후 작업 트리·임시 디렉터리 0개 |
| P3 | 위키 저장 형식, 낡음 판정, `wiki` op, `code_wiki` 작업 | 낡음 판정 테스트(blob 변경, 경로 삭제, 심볼 삭제, 새 파일), 인용 검증 연결 테스트, 미끼 검증 2 |
| P4 | Go API, 대시보드 Wiki 탭 | API 테스트, 웹 빌드 (구현함, 아래 "구현 메모 (P4)") |
| P5 | 평가 2차와 기본값 결정 | 완료(2026-10-17): 지도·위키 노출 모두 기본 켬 유지. `docs/analysis/2026-10-13-retrieval-eval2/results.md` |

## 검증

### 미끼 검증

- **미끼 1(관계 탐색):** 테스트 저장소에 같은 이름 `Handle`을 서로 다른 Go·TS 파일에 두고 호출 관계를 하나만 실제로 둔다. "X를 호출하는 곳"을 물었을 때 에이전트가 `refs` 결과를 `read`로 확인하고, 이름만 같은 쪽을 호출자로 인용하지 않는지 본다.
- **미끼 2(낡은 위키):** 위키 페이지가 "인증은 `auth.go#Check`에서 한다"고 말하지만 실제 코드는 다른 곳으로 옮겨진 상태(`broken`)를 만든다. 에이전트가 위키 문장을 근거로 인용하지 않고 코드를 다시 찾는지, 인용 검증이 위키만 보고 쓴 `path:N`을 잡는지 본다.

### 평가

고정 질문 12개를 같은 sha에서 실행해 비교한다. 같은 팀으로 세 구성을 돌린다: 기존(`GUARDIAN_REPO_MAP=0`, 위키 없음), P1+P2, P1+P2+P3.

- 질문 유형: API·핸들러 찾기 3, 호출 경로 추적 3, TS/TSX 화면 동작 3, 버그 수정(판정자 액션 포함) 3
- 지표(모두 기존 기록에서 읽는다):
  - `debate_finished.data.retrieval`의 단계 수·바이트
  - `citation_check`의 `unverified` 수
  - `action_repair` 발생 수와 최종 `INVALID` 수
  - jev 판정
  - 정답 일치(질문마다 사람이 정한 정답 위치)
- 판단 기준: 바이트가 줄고 정답 일치·jev가 같거나 나아지면 해당 기능을 기본으로 켠다. 정답 일치가 나빠지면 기본은 끈 채로 둔다.

## 결정 (2026-10-12, 모두 기본안)

1. **위키 저장 위치: 허브 데이터 디렉터리**(`<GUARDIAN_DATA_DIR>/code-wiki/<project>/`). 대상 저장소를 건드리지 않고, 프로젝트마다 쓰기 권한이 필요 없다.
   검토한 대안은 대상 저장소의 `docs/wiki/`에 커밋하는 것이었다. sha 고정과 git 리뷰를 얻지만, 쓰기가 publish 흐름을 거쳐야 하고 사용자 프로젝트에 파일이 생긴다.
2. **TS/TSX 파서: 표준 라이브러리 원칙 유지**(레이아웃 파서). prettier로 정리되지 않은 코드와 중첩 선언(컴포넌트 안의 함수)은 놓치는 것을 받아들인다. tree-sitter 같은 선택 의존성은 쓰지 않는다.
3. **위키 갱신: 수동만.** 대시보드 버튼이나 CLI `--stale`로만 다시 쓴다. 정해 둔 시각의 자동 갱신은 두지 않는다(LLM 비용).
4. **지도 상한: 기본 32KB**(`GUARDIAN_REPO_MAP_BYTES`). 프롬프트 크기 규칙이며 무제한인 탐색 예산과는 별개다. 지도 전체 크기는 P2에서 측정한다.
5. **개인 위키(`llm-wikis`): 분리.** 이 Mac의 `wiki` MCP와 허브 위키는 연결하지 않으며 내보내기·동기화도 두지 않는다.
6. **P2.6: Go부터 컴파일러 보조 인덱스**(2026-10-12 추가). 토론 엔진은 표준 라이브러리만 쓰는 원칙을 그대로 지키고, 사실 추출만 허브에 이미 있는 Go 툴체인과 표준 라이브러리 도우미(`goindex`)가 한다. 소스는 git에 유지하고, 추출에 쓴 작업 트리와 산출물은 매번 지운다. TS는 설치 스크립트 위험 때문에 별도 결정으로 미룬다.

## 이 설계가 하지 않는 것

- 임베딩·벡터 검색(의미 검색 op `search`)은 별도 실험으로 남긴다. 들어오더라도 후보 위치만 주는 2층 길잡이와 같은 규칙(근거 아님)을 따른다.
- LSP 서버는 띄우지 않는다. Go는 P2.6에서 일회성 타입 검사로 정확한 참조를 얻고, 그 밖의 언어의 `refs`는 이름 기반이며 결과에 그렇게 적는다.
- 위키 서술을 판정자의 액션 검증(`ContextPack.saw`)에 쓰지 않는다. 판정자는 지금처럼 읽은 원문으로만 패치를 만든다.

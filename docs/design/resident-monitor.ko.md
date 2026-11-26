# 대시보드의 상주 에이전트 모니터 (설계)

[English](resident-monitor.md) | 한국어

상태: 2026-11-21 제안, 같은 날 승인. 같은 날 운영자가 범위를 정했다: 상주 에이전트만 본다(토론 에이전트는 이미 실행 모니터가 있다),
워크플로 아래 새 탭, 우선 읽기 전용. RM1-RM4를 2026-11-21에 만들었다(로컬 허브로
실제 브라우저 확인), 2026-11-21에 `8e2179d`로 배포했다.
출발: "웹 대시보드에서 에이전트들의 동작과 상황을 모니터링할 수 있도록." 상주 에이전트(`docs/design/resident-agents.md`,
`docs/design/resident-open-tools.md`)는 허브의 메일 저장소와 작업 보드 위에서 돌지만 대시보드는 그중 아무것도 보여 주지
않는다. 운영자는 SSH로 `--residents`, `--mail-list`, `--tasks`와 릴레이 로그를 읽는다.

## 1. 목표와 비목표

목표: SSH 없이 한눈에 다음을 답하는 화면 하나.

- 어떤 상주 에이전트가 어디에, 어떤 provider, 드라이버, 도구(닫힘 / 열림)로 있는가.
- 각각이 `ready`, `down`, `retiring` 중 무엇인지, 언제부터인지, 어느 epoch인지, 바인딩이 몇 번 실패했는지.
- 각 에이전트가 최근 무엇을 했는가: 턴(종류, 시작, 길이, 끝난 이유, hop)과 메일(받은 것과 보낸 것).
- 작업 보드: 누가 무엇을 어떤 상태로 쥐고 있는지, 어떤 작업이 운영자를 기다리는 패치인지.
- 최근 저장소 이벤트(`agent_down`, `agent_restored`, `bind_failed`, `task_patch`, `mail_undeliverable`, ...).

비목표(이 설계):

- 쓰기 없음. 메일 보내기, 작업 만들기와 취소, 에이전트 제거는 다음 단계에서 각각 따로 결정한다. 승인을 기다리는 패치는
  기존 승인 화면으로 가는 링크만 둔다. 여기서는 아무것도 승인하지 않는다.
- 운영자 세션, 링크, 워크스테이션 없음(`docs/design/operator-sessions.md`): S1은 배포되지 않았다. 스냅숏에 자리만
  남기고(3장) 그 테이블은 읽지 않는다.
- 실시간 푸시(SSE, websocket) 없음. 턴이 수십 초 걸리는 에이전트에는 폴링으로 충분하다.
- 토론 에이전트 없음: 기존 실행 모니터(`pages/agents/*`, 토론 이벤트 스트림을 접어 만든다)는 그대로 둔다.

## 2. 제약

1. Go 에이전트는 표준 라이브러리만 쓰고 SQLite 드라이버가 없다. 팀, 워커, 흐름을 읽을 때처럼 Python CLI를 거쳐 저장소를
   읽는다(`h.guard(h.configGet("--flag"))`, `--json`을 붙인다).
2. 읽기 경로는 쓰면 안 된다. `MailStore._ready()`는 `_init_schema`(CREATE IF NOT EXISTS와 ALTER 마이그레이션)를
   실행한다. 모니터가 이를 부르면 상주 에이전트가 없는 허브에 파일을 만들고, 대시보드 폴링이 저장소를 마이그레이션할 수
   있다. 모니터는 SQLite URI `file:<path>?mode=ro`로 직접 열고 `_open`이나 `_ready`를 부르지 않는다. 그 위에 지은
   기존 도우미도 모두 부르지 않는다: `MailStore.events()`, `agent_rows()`, `list_mail()`, `mail_tasks.py`의 작업 보드
   읽기, `resident_registry.list_agents()`는 모두 `_ready()`를 거친다. `resident_registry.load_declarations()`는
   잘못된 항목이 있으면 `declaration_problem` 이벤트를 쓴다. 그래서 모니터는 선언 파일(`declaration_path()`)을 직접
   JSON으로 읽고 항목마다 `name`, `node`, `provider`, `driver`, `tools`만 가져온다. 잘못된 항목은 `problems` 한
   줄이 된다.
3. 폴링 한 번에 Python 프로세스 하나. 허브는 2 OCPU다. 몇 초마다 프로세스 다섯 개를 띄우는 화면은 부하에 드러난다.
   CLI는 묶은 스냅숏 하나를 돌려준다.
4. 메일 본문, 작업 패치, 호출 기록은 크고 코드를 인용할 수 있다. 스냅숏에는 메타데이터와 크기만 담고, 본문은 운영자가
   메일 하나를 열 때만 읽는다.
5. 실제 호스트 이름은 테스트, 픽스처, 미리보기에 넣지 않는다: `node-1`, `hub-1`, `claude@node-1`, `coder@node-1`.

## 3. 읽기 경로

새 모듈 `cli/llm-agent/resident_monitor.py` (`mail_store.py`는 바꾸지 않는다):

- `open_readonly(path) -> Optional[sqlite3.Connection]`: 파일이 없으면 `None`. 있으면
  `sqlite3.connect("file:...?mode=ro", uri=True)`, `busy_timeout` 5000, `row_factory` Row. WAL 읽기에는 `-wal`,
  `-shm` 파일이 있거나 만들 수 있어야 한다. 허브 저장소는 Go 에이전트와 같은 Unix 사용자로 WAL 모드에서 돌므로 이
  조건을 만족한다.
- 스키마: 표시는 `meta`(`key = 'schema_version'`, 문자열)에서 읽는다. 저장소는 `PRAGMA user_version`을 쓰지 않는다.
  SELECT마다 `PRAGMA table_info(<table>)`와 모니터가 원하는 열의 교집합으로 만든다. 테이블에 없는 열은 `null`로
  돌려주고, 테이블이 없으면 그 부분은 비우고 `problems`에 적으며 예외는 내지 않는다. 그래서 `SCHEMA_VERSION`보다
  오래되거나 새로운 저장소도 보이고, 표시가 `SCHEMA_VERSION`과 다르면 `problems`에 한 줄을 더한다.
- `snapshot(limits) -> dict`, 시각은 저장된 그대로(UTC 문자열)에 `generated`를 더한다:

```
{
  "generated": "...", "store": {"exists": true, "schema": 5, "path_kind": "data_dir"},
  "agents": [{
    "address", "name", "node", "provider", "driver", "tools",          # tools는 선언 파일에서
    "state", "epoch", "not_ready_since", "bind_failures", "bind_started", "modified",
    "incarnation": {"epoch", "started", "pane", "pid"} | null,          # 열린 것이 있으면
    "turn": {"id", "kind", "started", "hop_in"} | null,                 # `ended`가 없는 턴
    "mail_open": {"queued": n, "delivered": n, "retrieved": n},         # 이 에이전트에게 온 것
    "tasks_held": n,
    "declared": true, "in_store": true                                  # 선언됐지만 저장소에 없음, 또는 그 반대
  }],
  "turns": {"<address>": [{"id", "epoch", "kind", "started", "ended", "end_reason", "hop_in", "inputs": n}]},
  "mail": [{"id", "thread", "sender", "recipient", "kind", "subject", "task", "reply_to", "hop", "state",
            "created", "expires", "redelivered", "bytes"}],
  "tasks": [{"id", "title", "creator", "assignee", "owner", "status", "project", "sha", "hop", "generation",
             "blocked_by": [...], "patch_bytes", "patch_action", "calls": n, "reason", "created", "modified",
             "expires"}],
  "events": [{"id", "at", "kind", "agent", "detail"}],
  "problems": ["..."]
}
```

- 한도(상수, 테스트용 플래그로 바꿀 수 있다): 에이전트마다 턴 20개, 메일 100개(최신순), 열린 작업 전부와 닫힌 작업 중
  최신 50개, 이벤트 100개. 작업의 `detail`, `result`, `patch`, `calls_json`은 크기와 개수로, 메일 `body`는 `bytes`로
  줄인다. 크기는 바이트이며 SQL에서 `length(CAST(x AS BLOB))`로 잰다(그냥 `length()`는 글자 수를 센다). 이벤트
  `detail`은 그대로 넘긴다. 쓰는 쪽은 거기에 id, 개수, 상태, 이유를 넣고(`task_patch`는 작업, 액션, 파일 수, 바이트,
  base, `mail_sent`는 id, 받는 이, 종류, hop) 본문이나 패치는 넣지 않는다. 아래 테스트가 이를 지킨다.
- `mail_body(mail_id) -> dict`: 같은 읽기 전용 연결로 메일 하나를 본문과 함께 돌려준다. 본문은 `audit_run.mask_secrets`를
  거친다(열린 문제 2).

CLI(`guardian.py`의 상주 명령 옆에 연결): `--resident-monitor`(스냅숏)와 `--resident-mail-body ID`, 둘 다 `--json`을
따른다. 저장소가 없으면 빈 스냅숏(`store.exists: false`)으로 0을 돌려준다. 상주 에이전트가 없는 허브는 오류가 아니라
"상주 에이전트 없음"을 보여 준다.

나중에(이번 단계 아님): S1이 배포되면 같은 스냅숏에 `links`와 `sessions` 부분을 더한다.

## 4. 허브 API

- `GET /api/residents/monitor` -> `h.guard(h.configGet("--resident-monitor"))`.
- `GET /api/residents/mail/{id}` -> 작은 핸들러: `id`는 메일 id `m-YYYYMMDD-NNNN`이어야 한다(아니면 400). 그다음
  `runConfigCLI(nil, "--resident-mail-body", id, "--json")`.
- 둘 다 다른 모든 경로처럼 전역 `withSecurity(withAuth(mux))` 체인 뒤에 있다. 새 인증은 없다.

## 5. 대시보드

- 경로 `#/debates/residents`, 워크플로 세그먼트 컨트롤의 탭 "상주 에이전트", 모든 프로젝트 종류에 보인다(빠른 질문처럼
  상주 에이전트는 허브 전체의 것이다). `#/agents` 해시는 토론으로 가는 옛 리다이렉트로 남는다.
- 폴링: 탭이 보이는 동안 10초마다, 수동 새로고침 버튼, 마지막 스냅숏 시각 표시. `useHubData`는 지금 가시성 검사 없이
  `setInterval`로만 폴링한다. `document.visibilityState`에 따른 멈춤(다시 보이면 한 번 읽기)은 새로 넣는 것이며,
  다른 화면이 바뀌지 않도록 `useHubData`의 옵션으로 더한다.
- 경로: `'residents'`를 `DebatesPage.tsx`가 `DebatesView`의 `jobId`에서 빼는 탭 이름 목록(`wiki`, `artifacts`,
  `results`, `ask`, `templates` 옆)에 더한다. 그러지 않으면 탭 이름을 작업 id로 읽는다.
- 화면 구성:
  1. 에이전트 카드: 주소, 노드, provider, 드라이버, `tools`가 열림이면 "열린 도구" 칩, 상태 칩(`ready` / `down` /
     `retiring`, `not_ready_since`로 "언제부터"), epoch, 바인딩 실패 수, 진행 중인 턴(종류와 경과 시간) 또는 마지막
     턴의 끝, 열린 메일 수, 쥔 작업 수.
  2. 선택한 에이전트 상세(`?agent=<address>`): 최근 턴 표, 받은 메일과 보낸 메일(제목, 종류, 상태, hop, 시각. 행을
     누르면 본문을 그때 읽는다), 그 에이전트의 작업.
  3. 작업 보드: 상태, 제목, 담당, 프로젝트, 패치 크기. `patch_action`이 있는 작업은 `hrefFor('approvals',
     [patch_action])`로 링크한다. 메일 저장소는 액션 id만 알고 그 상태는 모른다(상태는 결정 DB에 있다).
     `patch_action`은 그 DB의 액션 id이며(`mail_relay.record_task_patch_action`이 쓴다), 승인 화면과 같은 id 공간이다.
     그래서 탭은 승인 화면이 이미 읽는 액션 목록을 불러와 클라이언트에서 맞춘다. 칩은 액션의 실제 상태(PROPOSED,
     APPROVED, REJECTED, ...)를 보이고, 목록에 없으면 상태 없이 "patch #id"만 보인다. 여기서는 아무것도 승인하지
     않는다.
  4. 이벤트 목록, 에이전트와 종류로 거른다.
- 모든 문자열은 두 카탈로그(`src/i18n/ko`, `src/i18n/en`)에 넣는다. 기본은 한국어. 상태 단어는 저장소 값을
  카탈로그의 라벨로 바꾼다(모르는 값은 그대로 보인다).

## 6. 단계

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| RM1 | `resident_monitor.py`, `test_resident_monitor.py`, `--resident-monitor` / `--resident-mail-body` | 단위 테스트 통과, 아래 읽기 전용 테스트 통과, Mac에서 `--resident-monitor --json` 한 번이 1초보다 충분히 짧다(아래 비용 점검) |
| RM2 | Go 경로와 핸들러 테스트 | `go test`가 id 검사와 CLI 인자를 다룬다 |
| RM3 | 워크플로 탭, ko/en 문자열 | `scripts/ci.sh web` 통과 |
| RM4 | 격리한 `GUARDIAN_DATA_DIR`에 `node-1` 픽스처를 채운 로컬 허브로 실제 브라우저 확인 | 에이전트, 턴, 메일, 작업, 이벤트가 보인다. 390 px 폭도 본다 |

RM1-RM3은 함께 배포한다. `guardian.py`는 그 파일의 진행 중인 S1 작업이 커밋된 뒤에만 고친다.

## 7. 테스트

- 읽기 전용: 저장소가 없으면 `snapshot()`이 `store.exists: false`를 돌려주고 그 뒤에도 파일이 없다. 저장소가 있으면
  `snapshot()`과 `mail_body()`가 도는 동안 `MailStore._open`, `MailStore._ready`, `resident_registry.load_declarations`가
  예외를 내도록 바꿔 둔다(`_init_schema` 끝의 표시 쓰기는 `INSERT OR REPLACE`라 파일 크기만 보면 호출을 놓칠 수
  있다). 그 뒤 `meta`의 표시, 파일 크기, `st_mtime_ns`가 그대로다. 돌려받은 연결로 쓰기를 시도하면 실패한다.
- 모양: 실제 `MailStore` API로 채운 저장소(에이전트 둘, 열린 턴 하나, 상태마다 메일, 패치가 있는 열린 작업과 끝난
  작업, 이벤트)가 문서의 키를 낸다. 한도는 최신 항목에서 자른다.
- 내용 없음: 메일 본문 하나와 작업 패치 하나에 고유한 표지 문자열을 넣는다. 그 문자열은 이벤트를 포함해
  `json.dumps(snapshot())` 어디에도 없고, 그 메일의 `mail_body()`에는 있다. 비 ASCII 글이 든 본문의 `bytes`는 UTF-8
  길이와 같다.
- 오래된 스키마: 버전 4 패치 열이 없는 저장소는 `patch_bytes`/`patch_action`을 `null`로 내고 표시에 대한 `problems`
  한 줄을 더한다. 테이블이 없으면 빈 부분과 `problems` 항목.
- 비용: `guardian.py --resident-monitor --json`은 모니터를 늦게(다른 상주 명령처럼 그 플래그 분기 안에서) 들여온다.
  비교용으로 2026-11-21 Mac에서 잰 값: `--teams --json` 0.16초, `--workers --json` 2.8초(호스트를 점검한다). 모니터는
  앞쪽에 머물러야 한다.
- 저장소에 없는 선언된 에이전트와 선언에 없는 저장된 에이전트가 둘 다 표시와 함께 보인다.
- Go: `m-YYYYMMDD-NNNN`이 아닌 id는 CLI를 띄우지 않고 400.

## 8. 열린 문제

1. 폴링 간격: 10초를 제안한다. 5초면 열린 탭마다 5초에 프로세스 하나가 더 든다.
2. 메일 본문: `audit_run.mask_secrets`(자격 증명처럼 보이는 대입의 값과 개인 키 블록을 `***`로 바꾼다)를 거쳐 보이도록
   제안한다. 2026-11-21에 확인: 저장소의 도우미 중 내용을 가리는 것은 그것뿐이다. `publish_ops.scrub`은 git 출력의 URL
   속 자격 증명만 가리고, `code_index.redact_literals`는 모든 문자열 리터럴을 지워 메일에는 읽을 수 없다.
   `operator-sessions.md`의 턴 요약 필터는 구현되지 않았다. 그것이 생기면 본문 경로를 바꿀 수 있다. 다른 안은 "저장된
   그대로"다. 대시보드는 `AGENT_TOKEN` 뒤에 있고 운영자는 이미 `--mail-list`로 본문을 읽을 수 있다.
3. 릴레이 상태(`guardian-mail-relay.service`가 살아 있는가)는 저장소에 없다. 다음 단계에서 CLI가 아니라 Go 쪽(거기서
   systemd를 읽을 수 있다)에서 더할 수 있다.

## 구현 메모

- 2026-11-21: 열린 문제 2는 운영자가 설계 승인과 함께 정했다. 메일 본문은 `audit_run.mask_secrets`를 거쳐 보인다.
  열린 문제 1은 제안대로 10초이고, 브라우저 탭이 가려져 있는 동안 폴링을 멈춘다.
- 2026-11-21: 설계는 처음에 메일 id를 양의 정수라고 적었다. 실제로는 문자열 `m-YYYYMMDD-NNNN`이다
  (`mail_store._mail_id`). 4절과 7절을 그에 맞게 고쳤다. Go 경로는 `^m-[0-9]{8}-[0-9]{4,9}$`, Python 쪽은
  `^m-\d{8}-\d{4,}$`(`re.ASCII`, Go처럼 전각 숫자를 거부)로 검사한다.
- 2026-11-21: `guardian.py` 연결: `handle_resident_monitor`가 `handle_resident_commands`보다 먼저 돈다. 그래서 모니터는
  `MailStore`를 열지 않고 `resident_registry.load_declarations`로 선언을 읽지도 않는다. `resident_monitor.cli_monitor` /
  `cli_mail_body`를 부르고 그 종료 코드로 끝난다(1 읽기 오류, 2 잘못된 id, 3 없음).
- 2026-11-21: RM3(웹 탭, z.ai가 만들고 claude가 검토). 5절과 다른 점: 메일 본문은 행 안이 아니라 에이전트 메일 표 아래에
  한 번에 하나씩 열린다. 턴 종류, `end_reason`, 이벤트 종류는 원래 값 그대로 고정폭 글꼴로 보이고, 카탈로그 라벨은
  에이전트·메일·작업 상태에만 붙는다(에이전트 상태 `bind_failed`는 검토 때 추가). 검토 수정: 이벤트 목록이 순서를
  뒤집었는데 스냅숏은 이미 최신순(`ORDER BY id DESC`)으로 보낸다. 뒤집기와 오래된 순 테스트 데이터를 없앴다.
- 2026-11-21: RM1(`resident_monitor.py`, z.ai가 만들고 claude가 검토, 단위 테스트 20개). 저장소는 `file:...?mode=ro`로
  열고 `MailStore`를 거치지 않는다(`MailStore`는 저장소를 만들거나 이전할 수 있다). 모든 SELECT는 `PRAGMA table_info`가
  알려 주는 열만 읽고 나머지는 `NULL`로 채운다. 그래서 예전 스키마(예: 패치 열이 없는 버전 3 tasks, `events` 테이블이 없는
  경우)에서도 답하고, 빠진 부분은 `problems`에 적는다. 선언 파일은 `resident_registry.load_declarations` 대신 일반
  JSON으로 읽는다. `store.path_kind`는 경로가 `GUARDIAN_DATA_DIR`에서 왔는지 기본값인지 알려 준다. 열린 작업은 오래된
  순, 끝난 작업은 최신 50개이고, 이벤트 `detail` 값도 메일 본문처럼 `mask_secrets`를 거친다.
- 2026-11-21: RM4. 격리한 `GUARDIAN_DATA_DIR`(`node-1` 에이전트 셋, 메일, 작업 셋(하나는 기록된 패치 포함), 이벤트)로 Mac의
  로컬 허브를 띄우고 실제 브라우저(내장 창)에서 확인했다. 본 것: 상태·epoch·마지막 턴이 있는 에이전트 카드, 작업
  보드(격리한 결정 DB에 그 액션이 없으므로 패치의 액션은 예상대로 "알 수 없음"), 최신순 이벤트 목록과 필터,
  `password=***`와 가림 안내가 붙은 메일 본문. 390 px에서 페이지 자체는 가로로 스크롤되지 않고, 카드와 작업 보드는 한 줄로
  쌓이며, 표는 각자의 상자 안에서 스크롤된다. 확인 중 고친 것: 짧은 칸(시각, 종류, 상태, 크기)이 390 px에서 글자 중간에서
  끊겨서 `DataTable` 열에 `nowrap`과 자유 텍스트용 `minWidth` 하한(제목 160 px, 이벤트 내용 200 px)을 더했다. 카드의
  "이 상태 N분 전"은 두 카탈로그 모두 "상태 변경 N분 전"으로 바꿨다. 머리글의 허브 칩이 가끔 오프라인이 되었는데, 한동안
  조용한 뒤 첫 `/api/status`가 로컬 허브에서 약 2초(대시보드의 상태 확인 제한 시간) 걸렸고 이후 호출은 0.07초였다. 이 탭은
  그 확인을 건드리지 않으므로 로컬 환경의 성질이지 이번 변경 때문이 아니다.
- 2026-11-21: 허브에 `8e2179d`로 배포했다(진행 중인 토론이나 solo 실행 없음). 허브에서 허브 토큰으로
  `GET /api/residents/monitor`가 실제 저장소에서 0.47초에 200을 돌려줬다(스키마 5, 에이전트 셋 모두 `ready`, 턴 3, 메일
  12, 작업 2, 이벤트 100, `problems` 없음). 실제 메일 본문 200, 형식이 틀린 id 400, 없는 id 404, 토큰 없음 401. 배포된
  탭 자체는 브라우저로 열어 보지 않았다(운영자 로그인이 필요하다).

# 사용자 정의 노드 그룹 (Python CLI ↔ Go hub API ↔ 대시보드)

[English](node-groups.md) | 한국어

## 1. 문제

`configs/servers.json`의 각 노드는 고정된 `group` 문자열 하나(예: `"Site B Cluster"`)만 가진다.
Fleet 화면(`FleetPage.tsx`의 `groupServers`), Assistant 집계(`AssistantPage.tsx`의 `groupRows`),
Go의 `selectNodes`(id/name/group 일치), CLI 내장 엔진의 클러스터 요약(`llm_runner.py`), CLI `--target`이
모두 이 문자열에 묶여 있다. 그룹을 만들거나 바꾸려면 servers.json을 직접 고쳐야 하고, 한 노드는 한 그룹에만 속한다.

목표:
- 이름 있는 그룹, 다대다(한 노드가 여러 그룹에 속할 수 있음).
- 멤버십 = 명시적 노드 id + 규칙(동적). 규칙에 맞는 새 노드는 자동으로 그룹에 들어간다.
- 허브 SQLite DB에 저장해 대시보드와 CLI에서 같은 그룹을 쓴다.
- servers.json의 `group` 값은 첫 초기화 때 정적 그룹으로 옮기고, 그 뒤로는 그룹핑에 쓰지 않는다
  (필드는 파일에 그대로 남긴다. servers.json을 다시 쓰지 않는다).

## 2. 데이터 모델과 DB 스키마

DB: 기존 허브 DB `GUARDIAN_DATA_DIR/guardian_decisions.db`(기본 `~/.guardian/data`, `GUARDIAN_DB_PATH`로 변경 가능).
`db_manager.DecisionDBManager._init_db`가 다른 테이블과 같은 방식(`CREATE TABLE IF NOT EXISTS`)으로 만든다.

```sql
CREATE TABLE IF NOT EXISTS node_groups (
    id TEXT PRIMARY KEY,              -- slug, ^[a-z0-9][a-z0-9_-]{0,63}$
    name TEXT NOT NULL,               -- 표시 이름 (1~64자)
    name_key TEXT NOT NULL UNIQUE,    -- casefold한 이름: 대소문자 무시 중복 금지
    description TEXT NOT NULL DEFAULT '',
    tone TEXT,                        -- 선택: ok | warn | crit | info | idle (디자인 시스템 톤)
    include_ids TEXT NOT NULL DEFAULT '[]',   -- JSON 배열: 명시적 서버 id
    rules TEXT NOT NULL DEFAULT '[]',         -- JSON 배열: 규칙 (3장)
    exclude_ids TEXT NOT NULL DEFAULT '[]',   -- JSON 배열: 마지막에 빼는 서버 id
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS guardian_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

정규화된 멤버십 테이블 대신 JSON 열을 고른 이유:
- 멤버십의 본체는 **규칙**(동적)이다. 실제 멤버는 매번 현재 servers.json에 대해 계산하므로 저장할 "멤버 행"이 없다.
  정규화하면 명시적 id만 별도 테이블에 들어가고, 규칙은 어차피 구조화된 JSON이 된다.
- 그룹 정의는 작고(노드 수십 대) 항상 통째로 읽고 쓴다. JSON 한 행이면 저장이 원자적이고 조인·다중 행 트랜잭션이 필요 없다.
- 검증은 Python 한 곳(`group_manager.py`)에서 하므로 DB 제약으로 막을 것은 id와 이름 중복뿐이며, 이 둘은 PRIMARY KEY와 `name_key UNIQUE`로 막는다.
- "노드 X가 속한 그룹" 같은 역조회도 규칙 평가가 필요해 SQL로 풀 수 없다. 노드·그룹 수가 작아 메모리 평가로 충분하다.

`guardian_meta`는 1회성 시드 여부(`node_groups_seeded`)를 기록한다. "그룹 테이블이 비어 있으면 시드"로만 판단하면
사용자가 그룹을 모두 지운 뒤 다음 실행에서 옛 클러스터가 되살아나기 때문이다.

백업: `db_backup.py`는 SQLite backup API로 DB 파일 전체를 복사하므로 새 테이블도 그대로 백업·복제된다. 변경 없음.

## 3. 규칙 의미

그룹 JSON (CLI stdin, API 본문, `--groups` 출력의 정의 부분):
```json
{
  "id": "arm-llm-hosts",
  "name": "ARM LLM hosts",
  "description": "Tier-1 ARM 노드 중 claude CLI가 있는 것",
  "tone": "info",
  "include": ["site-y-ec1"],
  "rules": [
    {"conditions": [
      {"field": "arch", "op": "eq", "value": "aarch64"},
      {"field": "cli_support", "op": "contains", "value": "claude"}
    ]},
    {"conditions": [{"field": "name", "op": "glob", "value": "site-b-*"}]}
  ],
  "exclude": ["site-a-arm1"]
}
```

멤버 = (`include` ∪ 어떤 규칙에든 맞는 노드) − `exclude`.
- 규칙 하나 = 조건들의 AND. 그룹 = 규칙들의 OR.
- `exclude`는 마지막에 적용되어 include와 규칙 결과 모두에서 뺀다.
- 결과 순서는 servers.json 순서.

필드와 연산자 (노드 JSON 필드 기준):

| field | 종류 | op | 의미 |
|---|---|---|---|
| `tier`, `arch`, `guardian_role`, `role` | 단일 값 | `eq`, `ne` | 대소문자 무시 같음/다름 (필드 없는 노드는 `eq`에 안 맞고 `ne`에 맞음) |
| `tailscale_tags`, `cli_support` | 목록 | `contains`, `not_contains` | 목록에 값이 있음/없음 (대소문자 무시) |
| `name` | 단일 값 | `glob`, `eq`, `ne` | `glob`은 `fnmatch` 패턴(`*`, `?`, `[..]`), 대소문자 무시 |

예:
- `tier eq Tier-1` → Tier-1 노드 7대.
- `[arch eq aarch64] OR [arch eq arm64]` → 모든 ARM 노드.
- `tailscale_tags contains tag:fleet` AND `guardian_role ne hub` → 허브를 뺀 fleet 노드.
- 규칙 없음 + include 3개 → 정적 그룹(시드된 클러스터가 이 형태).

검증(`group_manager.parse_group`):
- `name` 1~64자, 제어 문자 금지. `description` ≤ 500자. `tone`은 위 5개 중 하나 또는 생략.
- `id`는 생략하면 이름에서 slug(소문자, `[a-z0-9]` 외 문자는 `-`)를 만든다. 비ASCII 이름처럼 slug가 비면 `group-<sha1 8자>`.
  새 그룹의 slug가 이미 있으면 `-2`, `-3`… 을 붙인다. 이름을 바꿔도 id는 그대로다.
- `include`/`exclude` 각 ≤ 500개, 노드 id 형식 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, 중복 제거.
- `rules` ≤ 20개, 규칙마다 조건 1~10개(조건 없는 규칙은 모든 노드에 맞으므로 거부), 값 1~128자 문자열.
- 알 수 없는 키는 거부한다(오타가 조용히 무시되지 않도록).
- servers.json에 없는 id는 **저장은 허용**하고 경고로 알린다(노드를 잠시 뺐다가 다시 넣는 경우).

## 4. 마이그레이션 / 시드

`GroupManager`가 그룹을 처음 읽거나 쓸 때:
1. `guardian_meta.node_groups_seeded`가 있으면 아무것도 하지 않는다.
2. servers.json을 읽지 못하면 시드를 미룬다(표시는 하지 않음, 다음 호출에서 다시 시도).
3. 그룹 테이블이 비어 있으면 servers.json의 서로 다른 `group` 값마다 정적 그룹 하나를 만든다:
   `id = slug(group)`, `name = group`, `include = 그 group의 노드 id들`, 규칙 없음, `description = "Seeded from servers.json group"`.
   이미 그룹이 있으면(다른 경로로 만든 경우) 시드하지 않는다.
4. 표시를 남긴다. 이후 servers.json `group`은 그룹핑에 쓰지 않는다.

같은 이름이 여러 번 시드되어도 `INSERT OR IGNORE`라 안전하다(동시 실행 대비).

## 5. Python CLI 계약 (Go가 호출)

작업 디렉터리와 실행 방식은 [debate-teams-api.md](debate-teams-api.ko.md)와 같다. stdout은 JSON 한 개.

| 옵션 | stdin | 성공 (exit 0) | 실패 |
|---|---|---|---|
| `--groups --json` | - | 아래 참고 | - |
| `--group-save --json` | 그룹 JSON | `{"ok":true,"id":"...","members":[...],"warnings":[...]}` | exit 2 `{"ok":false,"error":"..."}` |
| `--group-delete ID --json` | - | `{"ok":true,"id":"..."}` | 없는 id는 exit 3 (HTTP 404) |
| `--group-preview --json` | 그룹 JSON (이름 불필요) | `{"ok":true,"members":[...],"warnings":[...]}` | exit 2 |

`--groups --json`:
```json
{
  "groups": [
    {"id": "site-b-cluster", "name": "Site B Cluster", "description": "...", "tone": null,
     "include": ["site-b-ec1", "..."], "rules": [], "exclude": [],
     "created_at": "...", "updated_at": "...",
     "members": ["site-b-ec1", "..."],
     "warnings": ["include: oci-old-1 is not in servers.json"]}
  ],
  "ungrouped": ["oci-new-1"],
  "catalog": {
    "fields": [
      {"field": "tier", "kind": "scalar", "ops": ["eq", "ne"], "values": ["Tier-1", "Tier-2"]},
      {"field": "tailscale_tags", "kind": "list", "ops": ["contains", "not_contains"], "values": ["tag:fleet", "..."]},
      {"field": "name", "kind": "scalar", "ops": ["glob", "eq", "ne"], "values": ["site-a-arm1", "..."]}
    ],
    "tones": ["ok", "warn", "crit", "info", "idle"]
  },
  "servers_file": "/home/ubuntu/.guardian/data/servers.json",
  "warnings": []
}
```
servers.json을 못 읽으면 `members`는 빈 배열, 최상위 `warnings`에 이유가 들어간다(가짜 멤버를 만들지 않는다).

servers.json 위치: `GUARDIAN_SERVERS_FILE` → 저장소 `configs/servers.json` → `~/.config/agentthread/servers.json`.
`GUARDIAN_SERVERS_FILE`이 설정되어 있으면 그 경로만 쓴다(파일이 없으면 그 경로를 이름으로 대며 실패한다).
허브 에이전트는 자신이 읽는 파일 경로를 `GUARDIAN_SERVERS_FILE`로 넘겨 두 쪽이 항상 같은 파일을 본다.
허브에서는 설치 스크립트가 `agent.env`에 `GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json`을 넣는다
([node-registration.md](node-registration.ko.md)). 저장소의 `configs/servers.json`은 git이 무시하는 로컬 스냅샷이며
(형식은 `configs/servers.example.json`), 저장소에 커밋되지 않는다.

## 6. Go hub API

모두 기존 토큰 인증과 `guard`(CLI 없으면 503) 아래. 본문 최대 64KB(`configBodyLimit`), 셸 없이 argv/stdin으로만 전달.

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/api/groups` | `--groups --json` 출력 그대로 |
| POST | `/api/groups` | 본문을 `--group-save` stdin으로. exit 2 → 400 |
| POST | `/api/groups/preview` | 본문을 `--group-preview` stdin으로 |
| DELETE | `/api/groups/{id}` | `{id}`는 `^[a-z0-9][a-z0-9_-]{0,63}$`, `--group-delete`. exit 3 → 404 |

Go는 멤버십을 모른다. UI와 CLI가 그룹을 노드 id 목록으로 풀어서 실행 API에 넘긴다:

| 경로 | 대상 지정 (이번에 추가된 것은 굵게) |
|---|---|
| `POST /api/fleet/exec/stream` | `{"command", "targets":[id...]}` (기존) |
| `POST /api/fleet/exec` | **`{"command", "timeout_sec"?, "targets"?:[id...]}`** |
| `GET /api/fleet/diagnose` | **`?targets=id1,id2`** (생략 시 전체) |
| `GET /api/fleet/ssh` | `?targets=id1,id2` (생략 시 전체; 나중에 추가, [fleet-ssh-audit.md](fleet-ssh-audit.ko.md)) |
| `POST /api/exec/remote` | `target_host` 하나 (기존) |

`targets`의 각 값은 servers.json의 id 또는 name과 정확히 같아야 하며, 하나라도 모르면 400이다.
(2026-10-18: `/api/exec/remote`의 `target_host`는 이제 인벤토리 id만 받는다. name이나 hostname은 400이다.
[node-registration.md](node-registration.ko.md) 7장.)

`selectNodes`는 더 이상 servers.json `group` 문자열을 받지 않는다. 그룹 개념을 하나(사용자 그룹)로 두기 위해서이며,
기존 호출자(UI는 `[]` 또는 노드 id, CLI는 대상 없음)는 group 문자열을 보내지 않으므로 깨지는 곳이 없다.
`/api/nodes`는 규칙 필드를 UI가 쓸 수 있도록 `tier`, `role`, `guardian_role`, `cli_support`도 내보낸다.

## 7. 대시보드

(2026-10-18: 처음 구현 뒤 대시보드 내비게이션이 바뀌었다([ui-flow-redesign.md](ui-flow-redesign.ko.md)). 아래는 현재 위치다.
Fleet 페이지 탭: 요약, 노드, SSH 감사, 명령, 그룹.)

- **노드 탭**(`#/fleet/nodes`): "Group by" 선택(User groups / None / Tier / Arch)과 그룹 필터. User groups에서는 한 노드가
  속한 모든 그룹 아래에 나타나고, 어느 그룹에도 없는 노드는 "Ungrouped"에 모인다. 그룹을 불러오지 못하면 None으로 보인다.
  하드코딩된 `groupServers`는 삭제. 행을 여러 개 골라 새 그룹을 만들거나 기존 그룹에 넣을 수 있고, `?group=<id>`이면 표 위에
  그룹 요약(정의, 멤버 수, 공유 진단 기준 도달성, 멤버의 fleet SSH 요약)을 보인다.
- **그룹 탭**(`#/fleet/groups`, `?edit=<id>`는 그 그룹의 편집 대화상자를 연다. 예전 `#/settings/groups`도 이리로 온다):
  그룹 목록과 멤버 수·경고, 새 그룹/편집/삭제.
  편집 대화상자: 이름, 설명, 명시적 멤버(검색 가능한 체크박스 목록), 규칙 편집기(규칙 카드 = AND 조건 행, 규칙끼리 OR;
  조건 행은 필드·연산자·값, 값은 catalog의 값 목록을 제안), exclude 목록, 실시간 멤버 미리보기(`/api/groups/preview`).
  삭제는 `ConfirmModal`로 확인.
- **노드 상세**(`#/fleet/nodes/<id>`): 그룹 멤버십 패널. 그룹마다 노드가 어떻게 들어 있는지(include로 추가 / 규칙으로 맞음)를
  보이고, 빼기는 include에서 지우거나 exclude에 넣는다.
- **명령 탭**(`#/fleet/commands`, 예전 Batch Exec): 대상 = 로컬 노드 / 각 노드 / 각 그룹 / 전체. 그룹 대상은 풀린 id 목록을
  스트리밍 실행의 `targets`로 보내며, 여러 대 실행이므로 전체와 같이 확인 대화상자를 거친다. 멤버가 0인 그룹은 선택할 수 없다.
- **Assistant**: 진단 결과를 사용자 그룹별로 집계했다(여러 그룹에 속한 노드는 각 그룹에 센다, 나머지는 Ungrouped).
  (2026-10-18: Assistant 화면은 없어졌다. 그룹별 도달성은 노드 탭의 그룹 요약이 보인다.)
- **SSH 감사 탭**(`#/fleet/ssh`): `/api/fleet/ssh`의 노드별 감사를 사용자 그룹별로 집계한다(설계:
  [fleet-ssh-audit.md](fleet-ssh-audit.ko.md)). `/api/ssh/*`는 허브 자신만 감사한다.

## 8. CLI `--target` 해석

`guardian.py --exec CMD --target X`, `--fleet --target X`, `--ssh-audit --target X`, 질의 모드 `--target X`:
1. `local`, `all`, `fleet`는 예약어(기존 의미).
2. servers.json(허브 `/api/nodes`)의 노드 id·name·hostname과 정확히 같으면 그 노드 한 대.
3. 사용자 그룹 id 또는 이름(대소문자 무시)이면 그 그룹의 멤버 id 목록. 멤버가 없으면 오류.
4. 아니면 오류: 비슷한 노드/그룹 이름(`difflib`)을 함께 보여주고 exit 2.

그룹 목록은 허브 `/api/groups`에서 가져온다(대시보드와 같은 결과). 허브에 그 API가 없거나 실패하면
CLI를 실행 중인 곳에 허브 DB 파일이 이미 있을 때만 그 DB로 직접 계산한다(CLI가 허브에서 실행될 때 같은 결과).
DB가 없는 곳에서는 빈 DB를 만들지 않고 그룹을 쓸 수 없다고 본다(노드 이름만 해석).

`--fleet --target <group>`은 그 멤버만 진단하고, `--exec CMD --target <group>`은 멤버에게만 스트리밍 실행한다.
내장 엔진의 클러스터 요약은 사용자 그룹별로 센다(여러 그룹에 속하면 각각, 없으면 "Ungrouped").

운영(ops) 프로젝트도 그룹을 대상으로 쓴다: 프로젝트의 `targets.groups`는 그룹 id 목록이며, 저장 시점이 아니라 토론 시작 시점과
액션 실행 시점에 각각 `group_manager.resolve_members`로 다시 풀린다(`cli/llm-agent/ops_targets.py`, 설계:
[server-ops-project.md](server-ops-project.ko.md)). 없는 그룹 id는 오류가 아니라 `missing`으로 보고된다.

## 9. 경계 조건

- **노드 이름 변경/삭제**: include/exclude는 id 기준. servers.json에서 사라진 id는 멤버에서 빠지고 그룹에 경고가 붙는다.
  정의에서 자동으로 지우지 않는다(노드가 돌아오면 다시 멤버가 된다).
- **빈 그룹**: 저장 가능, 멤버 0과 경고 "no servers match this group". Batch/CLI 대상으로는 쓸 수 없다(오류).
- **이름 중복**: casefold 기준으로 고유. 다른 id가 같은 이름을 쓰면 exit 2 "group name '...' is already used by <id>".
- **id와 노드 이름 충돌**: `--target`은 노드를 먼저 찾는다. 그룹 쪽을 쓰려면 다른 이름/ id를 쓴다.
- **시드 후 servers.json의 group 변경**: 반영하지 않는다(그룹은 DB가 관리).
- **동시 저장**: 마지막 저장이 이긴다(행 단위 upsert). 이름 고유성은 DB 제약이 보장한다.

## 10. 보안

- 모든 입력은 Python(`group_manager.parse_group`)에서 검증한다: 크기 64KB, 알 수 없는 키 거부, 길이·개수 상한, id 패턴, 제어 문자 금지.
- Go는 본문을 64KB로 자르고 stdin으로만 넘긴다. 경로의 `{id}`는 `idPattern`으로 검사 후 argv 원소로 전달(셸 없음).
- 규칙 `glob`은 `fnmatch`(정규식 아님)라 ReDoS가 없고, 값은 128자 제한.
- 실행 API의 `targets`는 Go가 servers.json과 정확히 대조한다. 그룹이 무엇을 담든 servers.json 밖으로 ssh가 나가지 않는다.
- SQL은 모두 매개변수 바인딩.

## 11. 후속 과제

- ~~노드별 SSH 감사 API가 생기면 SSH 화면을 그룹별로 집계한다.~~ 완료: [fleet-ssh-audit.md](fleet-ssh-audit.ko.md).
- `FleetNodeResult.group` 등 Go 응답의 레거시 `group` 필드는 UI에서 쓰지 않는다. 다른 소비자가 없으면 제거 가능.

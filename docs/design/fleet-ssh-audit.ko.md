# 노드별 SSH 보안 감사 (fleet SSH audit)

[English](fleet-ssh-audit.md) | 한국어

## 1. 문제

`/api/ssh/config`, `/api/ssh/security`, `/api/ssh/keys`는 허브 에이전트 프로세스가 도는 노드 한 대만 감사한다
(`apps/agent/sshd.go`: `ParseSSHDConfig` → `EvaluateSecurity`, `GetAuthorizedKeys`). 나머지 23개 노드의 sshd 설정과
`authorized_keys`는 볼 방법이 없고, 그래서 SSH 화면은 사용자 노드 그룹별 집계를 할 수 없었다
([node-groups.md](node-groups.ko.md) 7장·11장의 후속 과제).

허브는 이미 fleet 키로 모든 노드에 SSH 한다(`executeSSH`, `DiagnoseFleet`). 같은 경로로 **고정된 읽기 전용 프로브**를
돌려 노드마다 sshd 설정과 키를 가져오고, 허브 로컬 감사와 **같은 파서와 같은 규칙**으로 평가한다.

목표:
- 노드별 결과(설정 요약, 규칙 판정, 점수, authorized_keys)와 fleet 요약(평균 점수, 실패 노드, 규칙별 집계)을 한 API로.
- 원격에서 가져온 텍스트를 파싱할 수 있도록 `sshd.go`를 "파일/명령" 의존에서 "텍스트" 입력으로 분리.
- CLI `--ssh-audit`, 대시보드 SSH 화면에 그룹별 집계·규칙×노드 히트맵·노드 상세.
- 결과를 꾸며내지 않는다. 연결 실패, 일부만 읽힘, 실패를 그대로 상태로 보인다.

범위 밖: 설정 변경(액션)은 기존 Remediation/판정자 액션 경로가 맡는다. 이 기능은 읽기만 한다.

## 2. 원격 프로브

### 2.1 실행 방식

- `executeSSH(srv.ID, fleetSSHProbe, 20)`을 노드마다 호출한다. 키·호스트 처리는 기존 그대로(ssh-agent 소켓, `BatchMode`,
  `ConnectTimeout=4`, `--` 뒤 호스트). 새 키 처리는 없다. 호스트 키 정책은 노드마다 다르다: 대시보드에서 등록한(pinned) 노드는
  fleet 파일 블록의 엄격한 검사를 따르고, 블록이 없거나 이전(migrated) 블록인 노드만 `StrictHostKeyChecking=accept-new`를 받는다
  ([node-registration.md](node-registration.ko.md) 7장).
- 프로브는 Go 상수 하나다. **요청 값(대상 id, 쿼리)은 스크립트에 절대 들어가지 않는다.** 대상은 servers.json과 대조된
  `FleetNode.ID`로 ssh 목적지에만 쓰인다.
- 로그인 셸이 bash/dash/zsh 어느 것이든 같은 결과가 나오도록 `sh -c '<script>'`로 감싼다. 스크립트에는 작은따옴표가
  없어야 한다(테스트가 검사).
- 동시 실행은 최대 8개(세마포어). 24대 기준 3회 남짓의 물결이며, 노드당 상한 20초.
- 전체 출력은 `head -c 262144`(256 KiB)로 자른다. 잘리면 `END` 표지가 없어서 `partial`이 된다.

### 2.2 스크립트 (정확한 명령)

```sh
sh -c '
export LC_ALL=C
PATH="$PATH:/usr/sbin:/sbin:/usr/local/sbin"
m="@@GSA"
{
echo "$m BEGIN 1"
echo "$m USER"
id -un
got=0
for via in sudo direct; do
  if [ "$via" = sudo ]; then out=$(sudo -n sshd -T 2>&1 </dev/null); rc=$?
  else out=$(sshd -T 2>&1 </dev/null); rc=$?; fi
  if [ "$rc" -eq 0 ]; then
    echo "$m EFFECTIVE $via"
    printf "%s\n" "$out" | head -c 65536
    got=1
    break
  fi
  echo "$m EFFECTIVE_ERR $via $rc"
  printf "%s\n" "$out" | head -c 2048
done
if [ "$got" -eq 0 ]; then
  n=0
  for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*; do
    [ -f "$f" ] || continue
    n=$((n+1)); [ "$n" -le 32 ] || break
    if [ -r "$f" ]; then echo "$m FILE $f"; head -c 65536 "$f"; echo
    else echo "$m UNREADABLE $f"; fi
  done
fi
echo "$m KEYS"
if [ -r "$HOME/.ssh/authorized_keys" ]; then head -c 65536 "$HOME/.ssh/authorized_keys"; echo; fi
echo "$m END"
} 2>/dev/null | head -c 262144
'
```

순서와 이유:
1. `sudo -n sshd -T`: 유효 설정(Include·기본값 반영, 문법 검사 포함). `-T`는 호스트 키를 읽어야 해서 대부분 배포판에서 root가
   필요하다. fleet 로그인 사용자(ubuntu 등)는 보통 비밀번호 없는 sudo가 있으므로 이 경로가 주 경로다. `-n`이라 비밀번호를
   묻지 않고 바로 실패한다.
2. `sshd -T`: 로그인 사용자가 root인 노드.
3. 둘 다 실패하면 `cat`에 해당하는 읽기: `/etc/ssh/sshd_config`와 `/etc/ssh/sshd_config.d/`의 모든 파일(최대 32개, 파일당 64 KiB).
   `*.conf`로 좁히지 않는 이유: Include 패턴은 배포판마다 다르다(Ubuntu `*.conf`, macOS `*`). 어떤 파일을 쓸지는 Go 파서가
   실제 Include 패턴으로 고른다.
   읽을 수 없는 파일은 `UNREADABLE`로 표시만 한다(Ubuntu의 `50-cloud-init.conf`는 0600인 경우가 있다).
4. 로그인 사용자의 `~/.ssh/authorized_keys`(64 KiB).

### 2.3 출력 형식

`@@GSA `로 시작하는 줄이 구간 표지다. 표지 줄 다음부터 다음 표지 전까지가 그 구간의 본문이다.

| 표지 | 본문 |
|---|---|
| `@@GSA BEGIN 1` | 없음. 형식 버전 1. 없으면 프로브가 돌지 않은 것 |
| `@@GSA USER` | `id -un` |
| `@@GSA EFFECTIVE <sudo\|direct>` | `sshd -T` 출력 |
| `@@GSA EFFECTIVE_ERR <via> <rc>` | 실패한 `sshd -T`의 메시지(2 KiB) |
| `@@GSA FILE <path>` | 설정 파일 내용 |
| `@@GSA UNREADABLE <path>` | 없음 |
| `@@GSA KEYS` | authorized_keys 내용 |
| `@@GSA END` | 없음. 없으면 출력이 잘렸거나 중단됨 |

설정 파일 줄이 우연히 `@@GSA `로 시작할 일은 없다. 노드의 root가 의도적으로 넣으면 그 노드 자신의 결과만 왜곡된다
(8장). Go 파서는 모르는 표지를 무시하고, 같은 구간이 반복되면 첫 번째만 쓴다.

## 3. 파싱·평가 재사용 (`sshd.go` 리팩터링)

허브 로컬과 원격이 같은 코드를 쓰도록 입력을 텍스트로 바꾼다.

- `SummaryFromEffectiveConfig(text, path)`: `sshd -T` 출력 → `SSHConfigSummary`(Source `sshd -T`, 문법 검사됨).
  로컬 `ParseSSHDConfig`도 `runSSHD` 결과를 이 함수로 넘긴다.
- `readSSHDConfig(fsys, path, values, depth, includes)`: 파일 접근을 `sshdConfigFS`(ReadFile, Glob)로 추상화.
  - 로컬: `osConfigFS`(os.ReadFile, filepath.Glob) — 기존 동작 그대로.
  - 원격: `mapConfigFS`(프로브가 보낸 `path → 내용`). Glob은 `filepath.Match`로 맵 키를 찾는다.
  - 규칙은 기존과 같다: 디렉티브 첫 값이 이긴다, `Include`를 따라간다(상대 경로는 `/etc/ssh/` 기준, 정렬된 순서),
    `Match` 이후는 파일 끝까지 조건부라 건너뛴다(Match 안의 `Include`도 건너뜀), 포함된 파일의 Match는 포함한 파일로 새지 않는다.
    `key=value` 형식과 대소문자 무시.
- `ParseSSHDConfigText(mainPath, files)`: 원격 폴백용. 따라간 Include 패턴을 함께 돌려줘서, 프로브가 가져오지 않는 위치
  (`/etc/ssh/sshd_config.d/` 밖)를 가리키는 Include는 노드 `notes`에 "포함 파일을 읽지 못함"으로 남긴다.
- `isSSHDConfigError`: 원격 `EFFECTIVE_ERR` 메시지에도 그대로 쓴다. sshd가 실제로 설정을 거부한 경우만 "문법 오류"로,
  sudo 거부·호스트 키 없음은 "검사 안 됨"으로 본다.
- `EvaluateSecurity`, `parseAuthorizedKeyLine`, `keyFingerprint`는 변경 없이 재사용.

`/api/ssh/config|security|keys`의 동작과 기존 테스트는 그대로다.

## 4. 노드 상태 판정

| 상태 | 조건 | config/report |
|---|---|---|
| `ok` | `sshd -T` 성공, `END` 있음 | 있음 |
| `partial` | 폴백 파일로 평가했음(Source `sshd_config (fallback)`), 또는 `END` 없음(잘림·시간 초과) | 있음 |
| `unreachable` | `BEGIN` 없음 + ssh 종료 코드 255 또는 시간 초과로 종료 | 없음 |
| `error` | `BEGIN` 없음(그 밖의 종료), 또는 `sshd -T`도 파일도 읽지 못함 | 없음 |

`partial`의 폴백 결과는 Include 대상 중 읽지 못한 파일과 sshd 기본값 변화가 반영되지 않을 수 있다.
이 사실을 `notes`에 적고, 문법은 "검사 안 됨"으로 둔다(설정 거부 메시지가 있으면 "문법 오류"). 평가 불가인 노드에 기본값으로
점수를 매기지 않는다. 점수가 없으면 평균에서도 빠진다.

상태와 별도로 각 노드는 `reachability`를 갖는다. fleet 진단, 명령 실행, 등록 프로브와 같은 분류(`classifySSH`,
`apps/agent/ssh_classify.go`)다: `ok`, `auth_failed`(노드가 fleet 키를 거부), `host_key_changed`(호스트 키 검증 실패),
`unreachable`(연결 없음, 시간 초과), `command_failed`(ssh는 연결됐지만 프로브 표지가 없음). `status`는 감사 의미를 그대로
유지하고, `reachability`는 `unreachable`/`error` 노드가 왜 실패했는지를 화면마다 같은 말로 보여준다.

## 5. API 계약

`GET /api/fleet/ssh?targets=a,b[&refresh=1]` — 다른 fleet API와 같은 토큰 인증(`withAuth`)과 `withSecurity` 아래.

- `targets` 생략(또는 빈 값) = servers.json 전체. 각 값은 id 또는 name과 정확히 같아야 하며 하나라도 모르면
  **SSH를 시작하기 전에** 400(`fleetSubset`, `fleetErrorStatus`). servers.json을 못 읽으면 500.
- GET 외 메서드는 405.

응답:

```json
{
  "generated_at": "2026-10-05T03:00:00Z",
  "duration_ms": 2310,
  "cached": false,
  "nodes": [
    {
      "id": "site-a-arm1", "name": "site-a-arm1",
      "status": "ok",
      "reachability": "ok",
      "source": "sshd -T (sudo)",
      "login_user": "ubuntu",
      "duration_ms": 640,
      "config": { "...": "SSHConfigSummary" },
      "report": { "...": "SecurityReport" },
      "keys": [ { "id": "key-1", "key_type": "ssh-ed25519", "public_key": "", "comment": "...", "fingerprint": "SHA256:...", "options": "from=\"100.64.0.7\"" } ],
      "notes": [],
      "error": ""
    }
  ],
  "summary": {
    "nodes": 24, "audited": 22, "unreachable": 1, "errors": 1,
    "avg_score": 71.4,
    "failing_nodes": ["site-b-ec1"],
    "rules": [ { "rule_id": "SEC-SSH-001", "title": "Disable Direct Root Login via SSH" } ],
    "by_rule": { "SEC-SSH-001": { "fail": 2, "warn": 18, "pass": 2, "info": 0 } }
  }
}
```

- `nodes` 순서는 servers.json 순서.
- `reachability`는 4장의 공통 ssh 분류다.
- `keys`는 공개키 본문(`public_key`)을 비운다. 지문·주석·옵션으로 충분하고 응답 크기를 줄인다. 개인 데이터는 애초에 없다.
- `avg_score`: 평가된 노드(`ok`+`partial`)의 평균, 소수 첫째 자리. 평가된 노드가 없으면 `null`.
- `failing_nodes`: FAIL 규칙이 하나라도 있는 노드 id(WARN/INFO만 있으면 제외 — 로컬 화면의 "failed" 정의와 같다).
- `rules`: 규칙 카탈로그(평가 순서). 히트맵 열 순서로 쓴다.
- `by_rule`: 규칙별 상태 개수(평가된 노드만).
- 그룹별 집계는 Go가 하지 않는다. Go는 그룹을 모르며([node-groups.md](node-groups.ko.md) 6장), UI·CLI가 `/api/groups` 멤버십으로 묶는다.

## 6. 캐시

허브에 **60초 TTL 메모리 캐시**를 둔다. 키는 해석된 노드 id 집합(정렬, 전체는 그 id 전부). `?refresh=1`은 캐시를 건너뛰고
새로 돌린 결과로 캐시를 갱신한다.

이유:
- 한 번 요청에 24대 SSH와 `sudo sshd -T`가 돈다. 대시보드는 화면 진입·전역 Refresh·노드 상세 화면마다, CLI·Security
  Auditor 에이전트는 질의마다 이 API를 부른다. 여러 탭/사용자가 동시에 열면 같은 팬아웃이 겹친다.
- sshd 설정은 분 단위로 바뀌지 않는다. 60초 지연은 감사 용도에 문제가 없고, 방금 고친 직후에는 Refresh(`refresh=1`)로 확인한다.
- 같은 키로 이미 실행 중인 요청이 있으면 새로 SSH 하지 않고 그 결과를 함께 기다린다(single-flight). `refresh=1`도
  진행 중인 실행에는 합류한다(그 결과가 이미 최신이므로).
- 응답에 `cached`와 `generated_at`을 넣어 화면이 결과의 나이를 그대로 보여준다.
- 항목은 최대 64개, 저장할 때 만료 항목을 지우고, 그래도 가득 차 있으면 가장 오래된 항목을 지운다. 프로세스 메모리 전용이라
  재시작하면 비워진다(디스크에 키 목록을 남기지 않음).

검토한 대안: 캐시 없음(팬아웃 중복, 대시보드 응답 2~5초), 주기적 백그라운드 수집(아무도 보지 않을 때도 24대에 sudo를 돌림,
허브 상태 증가). 요청 시 계산 + 짧은 캐시가 가장 단순하다.

## 7. 실패 모드

| 상황 | 결과 |
|---|---|
| 노드 꺼짐, 네트워크 단절, 호스트 키 불일치 | ssh 255 → `unreachable`, stderr 마지막 줄을 `error`에. `reachability`는 `unreachable`, `host_key_changed`, `auth_failed`로 구분 |
| ssh-agent 소켓 없음(허브 fleet 키 서비스 중단) | 모든 노드 `unreachable`, 같은 오류 메시지. 로컬 섹션은 계속 동작 |
| fleet 파일(`~/.ssh/guardian_fleet.conf`)이 있지만 읽을 수 없음 | `executeSSH`가 ssh를 시작하지 않고 255로 거부 → 모든 노드 `unreachable` |
| 20초 초과 | 프로세스 종료 → `BEGIN`이 없으면 `unreachable`, 있으면 `partial`("output incomplete") |
| `sudo -n` 불가, 로그인 사용자 비root | 폴백 파일 → `partial`, notes에 sudo 메시지 |
| sshd_config 0600(RHEL 계열) + sudo 불가 | `error` "no readable sshd configuration" |
| sshd 설정 오류 | `sudo sshd -T`가 거부 → 폴백 평가 + `syntax_checked=true, is_valid=false` + 메시지 |
| sshd 미설치 | `sshd -T` 실패, 파일도 없음 → `error` |
| 출력 256 KiB 초과 | 잘림 → `END` 없음 → `partial` |
| authorized_keys 없음 | `keys: []` (정상) |
| servers.json 없음 | 500, SSH 없음 |
| 알 수 없는 target | 400, SSH 없음 |

## 8. 보안

- 프로브는 Go 상수이며 입력 보간이 없다. 요청이 바꿀 수 있는 것은 대상 집합(servers.json으로 검증)과 캐시 우회뿐이다.
- 프로브는 읽기만 한다: `sshd -T`(설정 출력), `head`, `id`. 파일을 쓰거나 서비스를 건드리지 않는다.
  `sudo -n`은 비밀번호를 묻지 않는다.
- 노드 출력은 신뢰하지 않는다: 크기 상한(프로브 256 KiB), 구간별 상한, 줄 단위 파싱, 정규식 없음. 노드가 결과를 위조해도
  자기 행만 바뀌고 다른 노드나 허브 명령에는 영향이 없다(출력을 셸에 다시 넣지 않음).
- 응답은 공개키 본문도 빼고 지문만 준다. authorized_keys의 `command="..."` 옵션은 그대로 보이므로, 옵션에 비밀을 넣지 않는다는
  기존 운영 원칙을 따른다.
- 인증: 기존 `withAuth`(Bearer 토큰)와 `withSecurity`(Host/Origin/CORS) 아래. GET이지만 부작용은 SSH 읽기뿐이고,
  캐시가 반복 호출의 팬아웃을 막는다.

## 9. 대시보드 (SSH 화면)

SSH 화면은 Fleet 페이지의 SSH 감사 탭(`#/fleet/ssh`, 예전 `#/ssh`·`#/security`도 열린다)이다
(`apps/web-desktop/src/pages/SshPage.tsx`, `pages/ssh/FleetSshAudit.tsx`).

- **Fleet SSH audit** 패널(맨 위). 화면 진입 시와 전역 Refresh 때 조회한다(허브가 60초 캐시로 답할 수 있다).
  패널의 감사 새로 고침 버튼은 `refresh=1`. 결과 시각, `cached` 여부, 걸린 시간을 표시.
  - 요약 카드 세 개: 평가 노드/전체(감사하지 못한 노드가 있으면 `reachability`별 개수, 예: "호스트 키 변경 1개 · 접속 불가 1개 ·
    오류 2개"), 평균 점수, 실패 노드 수.
  - **그룹 요약 표**: 사용자 그룹마다 노드 수, 평가 수(연결 불가 수 포함), 평균 점수, 실패 노드(노드 페이지 링크), 가장 많이
    실패한 규칙. 여러 그룹에 속한 노드는 각 그룹에 센다, 그룹 없는 노드는 Ungrouped. 그룹을 못 불러오면 오류(다시 시도)와 함께
    전체 한 줄로.
  - 그룹 필터(`lib/groups.ts`의 `filterByGroup` 재사용): 전체 / 각 그룹 / Ungrouped. 멤버가 없는 그룹은 고를 수 없다.
  - **규칙×노드 히트맵**(`Heatmap`, 디자인 시스템): 행 = 노드(필터가 전체이면 그룹별로 나눠 표시), 열 = 규칙 카탈로그,
    칸 = P/W/F/I(INFO, 예: 기본 포트 22)/N(보고서에 그 규칙이 없음: 지어낸 PASS 대신 "검사 안 됨"). `partial` 노드는 행에
    상태 칩이 붙는다. 평가하지 못한 노드는 칸 대신 상태와 오류를 한 줄로 보이는 "사용 불가" 행이 된다. 행을 누르면 그 노드를 선택한다.
  - **노드 상세**: 선택한 노드(기본값은 첫 실패 노드, 없으면 첫 노드) → 상태·`reachability` 칩·점수·출처·로그인 사용자·notes,
    sshd 거부 메시지, FAIL/WARN/INFO 규칙(현재값 vs 기대값, 해결 방법), authorized_keys 표. 노드 페이지 링크와 "이 노드 토론" 버튼
    (그 노드의 발견 사항으로 토론 작업을 미리 채우고 Debates로 이동)이 있다. 같은 컴포넌트(`NodeDetails`)를 노드 상세 페이지
    (`#/fleet/nodes/<id>`)가 그 노드 하나만 감사해서 쓴다.
- 기존 허브 로컬 섹션은 "Hub agent (local view)" 접이식 영역(`Disclosure`)으로 내린다. 허브 자신도 fleet 행으로 나오지만,
  로컬 섹션은 fleet SSH(ssh-agent 소켓)가 멈춰도 동작하고, 허브 에이전트 프로세스가 보는 설정을 보여주므로 남긴다.
- 좁은 화면: 표·히트맵은 가로 스크롤(`DataTable`, `.sg-heatmap-wrap`), 셀렉트는 줄바꿈.

(2026-10-18: 처음 설계는 요약 카드 네 개, P/W/F만 있는 히트맵, 노드 선택 셀렉트, 히트맵 아래의 별도 "감사하지 못한 노드" 목록이었다.
`Heatmap`에 INFO·"검사 안 됨"·사용 불가 행·행 선택이 생기면서 위처럼 바뀌었다.)

## 10. CLI

- `agent_client.NodeAgentClient.fleet_ssh_audit(targets=None, refresh=False)` → `GET /api/fleet/ssh`(제한 시간 90초).
- `guardian.py --ssh-audit [--target X] [--refresh] [--json]`
  - `--target`은 기존 해석(노드 → 그룹, `local`/`all`/`fleet`는 전체).
  - 사람용 출력(`ssh_audit.render_ssh_audit`): 그룹별 표(그룹, 노드, 평가, 평균 점수, 실패 노드) → 노드별 FAIL 규칙과 해결 방법 →
    부분 결과 노드와 notes → 감사하지 못한 노드. 여러 그룹에 속한 노드는 각 그룹에 센다, 그룹 없는 노드는
    "Ungrouped"(`group_manager.memberships`). 그룹을 불러오지 못하면 경고와 함께 전체를 한 묶음으로 보인다.
  - `--json`은 API 응답에 `groups`(그룹 이름 → 멤버 id, 그룹을 못 불러오면 `null`)와 `group_rows`(그룹별 요약 행)를 붙여 출력.
  - 종료 코드: 0(감사 완료, 결과와 무관), 1(허브 호출 실패), 2(잘못된 `--target`).
- Security Auditor 에이전트: 질의가 fleet/그룹/원격 노드 대상이면 `/api/fleet/ssh` 결과(요약·그룹별 멤버십·노드별 실패 규칙)를
  LLM 컨텍스트로 쓰고(`ssh_audit.llm_context`), 로컬 대상이면 기존처럼 허브 로컬 API를 쓴다.

## 11. 후속 과제

- ~~`Heatmap`에 "평가 안 됨"·INFO 상태와 행 선택 콜백이 생기면 연결 불가 노드를 같은 표에 넣고 행 클릭으로 상세를 연다.~~
  완료(2026-10-18 확인): 9장.
- `AuthorizedKeysFile`이 기본값이 아닌 노드: `sshd -T`의 `authorizedkeysfile` 값을 읽어 그 경로를 프로브하도록 확장.
- 규칙 확장(예: `KexAlgorithms`/`Ciphers` 약한 알고리즘, `LoginGraceTime`, `ClientAliveInterval`). `sshdKeys`와 `EvaluateSecurity`만 바꾸면 된다.
- Remediation 에이전트의 대상을 노드별 감사 결과로 확장(현재는 허브 로컬 규칙 기준).

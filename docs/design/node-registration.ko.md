# 허브 SSH 설정을 통한 노드 등록

[English](node-registration.md) | 한국어

상태: 승인(2026-10-07), 백엔드와 대시보드 구현 완료(6~8장이 실제 API를 설명한다). 범위: `apps/agent`, `apps/web-desktop`, `scripts/setup-hub-ssh.sh`.

## 1. 왜

허브 에이전트는 이미 `ssh -- <id> <cmd>`(`sshArgs`)로 접속하므로, 노드가 어디에 있는지는 허브의 `~/.ssh/config`가 정한다.
`configs/servers.json`은 `hostname`, `user`, `port`, `key`, `tailscale_ip`를 되풀이하고, `setup-hub-ssh.sh`가 이를 한 방향으로
허브 `~/.ssh/config`의 관리 블록에 복사한다. 노드를 등록하려면 Mac에서 JSON을 고치고, 스크립트를 돌리고, 다시 배포해야 한다.

결정: 연결 설정은 한 곳, 허브가 소유하는 SSH config 문법의 fleet 파일에 둔다. `servers.json`은 노드 목록(명령의 허용 목록)과
그룹 규칙이 맞춰 보는 메타데이터를 유지한다. 노드는 대시보드에서 등록한다.

## 2. 허브의 파일

- `~/.ssh/guardian_fleet.conf`: 노드마다 `Host` 블록 하나. agent만 쓴다(이전 중에는 `setup-hub-ssh.sh`도 쓴다). 권한 600.
  경로 재정의: `GUARDIAN_SSH_FLEET_FILE`.
- `~/.ssh/config`: 맨 위 가까이에 `Include ~/.ssh/guardian_fleet.conf`가 있다(전체에 적용되려면 Include가 어떤 `Host` 블록보다
  앞서야 한다). agent는 이 파일에 다른 것을 쓰지 않는다. Include 줄이 없으면 그 사실을 알리고, UI는 추가할 한 줄을 보여준다.
- `~/.guardian/data/servers.json`: 허브의 인벤토리. `configs/servers.json`과 같은 형식이며 노드마다 `id`를 키로 한 항목 하나,
  `id`는 `Host` 별칭과 같다. 허브는 `agent.env`에서 `GUARDIAN_SERVERS_FILE`을 이 파일로 설정하므로(토론 CLI도 이 경로를 물려받는다)
  agent는 자기가 실행되는 릴리스에 쓰지 않는다(`~/.guardian/current`, install-tracks R1부터 읽기 전용). 연결 필드는 호환을 위해
  선택 사항으로 남고, fleet 파일에 그 노드의 블록이 있으면 agent는 fleet 파일의 값을 우선한다.
- 저장소의 `configs/servers.json`은 시드이자 관리용 Mac의 사본이 된다. Mac의 스크립트는 계속 이 파일을 읽고,
  `scripts/pull-inventory.sh`가 허브 인벤토리를 다시 가져와서 대시보드에서 한 등록을 검토하고 커밋할 수 있게 한다.
  (2026-10-18: `configs/servers.json`은 이제 git이 무시하는 로컬 스냅샷이라 실제 노드가 저장소에 들어가지 않는다.
  형식은 `configs/servers.example.json`이 보여준다. 관리 스크립트는 기본으로 허브 인벤토리를 ssh로 읽고, `--offline`일 때만
  스냅샷을 쓴다. `scripts/pull-inventory.sh`는 스냅샷을 갱신할 뿐 아무것도 커밋하지 않는다.)

대시보드에서 등록한 노드에 대해 agent가 쓰는 블록은 정확히 이 모양이다:

```
Host <id>
    HostName <host>
    User <user>
    Port <port>
    IdentityFile ~/.ssh/guardian_fleet.pub
    IdentityAgent /run/guardian-fleet/agent.sock
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    UserKnownHostsFile ~/.ssh/guardian_known_hosts
    HostKeyAlias <id>
```

등록 때 고정(pin)한 호스트 키(4장)와 함께 `StrictHostKeyChecking yes`가 등록 노드의 `accept-new`를 대신한다.
`setup-hub-ssh.sh`가 이전한 블록은 처음 일곱 줄이 같고 그 뒤에 `StrictHostKeyChecking accept-new`가 오며
`UserKnownHostsFile`/`HostKeyAlias`가 없다("migrated" 모양). 이 블록은 호스트 키를 다시 확인할 때까지(hostname이나 port를 바꾸는
수정, 또는 `host_key_fingerprint`만 담은 `PUT`(6장). 둘 다 블록을 registered 모양으로 다시 쓴다) `accept-new`를 유지한다.
agent는 정확히 이 두 모양만 알아본다. 그 밖의 블록은 "unmanaged"다(3장).

### 호스트 키 저장: 왜 `HostKeyAlias`인가

`HostName`이 설정되어 있으면 ssh는 서버 키를 `Host` 별칭이 아니라 *HostName*(22가 아닌 포트면 `[host]:port`)으로 찾는다.
그래서 고정된 항목은 주소나 포트가 바뀔 때마다 조용히 깨지고, 찾는 이름이 포트에 따라 달라진다. OpenSSH로 확인한 사실:
`HostKeyAlias <id>`가 있으면 찾는 이름은 정확히 `<id>`다(포트 2222에서도 `[host]:port` 형태가 아닌 그대로). `[127.0.0.1]:2299`로
저장된 항목은 찾지 못한다. 그래서 agent는 고정 키를 `~/.ssh/guardian_known_hosts`에 다음처럼 저장하고

```
<id> <keytype> <base64 key>
```

블록에 `HostKeyAlias <id>`를 넣는다. 운영자가 지문을 확인한 키만 저장한다(스캔이 돌려준 모든 키가 아니다). 노드를 지우면
호스트 필드가 정확히 `<id>`인 줄만 지운다. 다른 줄(그리고 `@cert-authority`/`@revoked` 표지)은 건드리지 않는다.

## 3. 검증 (보안 경계)

대시보드 세션이 fleet 파일을 허브의 명령 실행으로 바꿀 수 있어서는 안 된다(`ProxyCommand`, `LocalCommand`, `Match exec`,
줄바꿈 주입). 규칙:

- 요청에서 오는 것은 `HostName`, `User`, `Port`뿐이다. 나머지 줄은 모두 agent가 고정한다.
- `id`: `^[a-z0-9][a-z0-9-]{0,62}$`, fleet 파일과 `servers.json` 전체에서 고유.
- `HostName`: IPv4 주소, 또는 DNS 이름 `^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$`. 공백, `%`, 따옴표, 앞의 `-`는 안 된다.
- `User`: `^[a-z_][a-z0-9_-]{0,31}$`. `Port`: 정수 1..65535.
- 쓰기 전마다 파일을 파싱한다. 고정 모양에 맞지 않는 블록(손으로 고친 것)은 바이트 그대로 보존하되 `unmanaged`로 표시하고 다시
  쓰지 않는다.
- 쓰기는 원자적이며(임시 파일 + rename), fleet 잠금(프로세스 뮤텍스와 `guardian_fleet.conf.lock`에 대한 권고 `flock`,
  `setup-hub-ssh.sh`도 같은 잠금을 잡는다) 아래에서 하고, `guardian_fleet.conf.bak`을 남긴다.
- 쓴 뒤 agent는 `ssh -G <id>`를 돌려 유효한 `hostname`, `user`, `port`가 요청과 같은지 확인한다. 다르면 백업을 복원하고 실패한다.

메타데이터 필드(`tier`, `role`, `guardian_role`, `cli_support`, `tailscale_tags`, `description`)는 그룹 규칙 catalog처럼
검증한다: 짧은 문자열, 짧은 문자열의 목록. `guardian_role`의 값은 `hub`(예약됨, 정확히 하나. UI에서 설정하거나 지울 수
없다)와 `node`이다. 오래된 인벤토리에는 `agent`가 남아 있을 수 있는데, `node`와 같게 동작한다(모든 소비자는 `hub`만
가려낸다).

## 4. 등록 흐름

API(모두 기존 세션 guard 아래, JSON):

| 단계 | 엔드포인트 | 효과 |
|---|---|---|
| 허브 정보 | `GET /api/nodes/registration` | 허브 fleet 공개키 줄(`from="<hub tailscale ip>",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 ...`), `~/.ssh/config`가 fleet 파일을 포함하는지, fleet 파일 경로 |
| 확인 | `POST /api/nodes/check` `{id, hostname, user, port}` | 검증한 뒤 `ssh-keyscan -T 5 -p <port> <hostname>`. 호스트 키 지문(SHA256)을 돌려준다 — 아무것도 쓰지 않는다 |
| 등록 | `POST /api/nodes` `{id, hostname, user, port, host_key_fingerprint, metadata}` | 다시 스캔해 지문이 맞는지 요구한다. 호스트 키를 `~/.ssh/guardian_known_hosts`(블록의 `UserKnownHostsFile`이 가리킴)에, `Host` 블록을, `servers.json` 항목을 쓴다. 그다음 노드를 프로브한다(`ssh <id>`로 `probeCommand` 실행: `uname -m`과 비밀번호 없는 sudo 확인) |
| 수정 | `PUT /api/nodes/{id}` | 같은 검증으로 연결 필드와/또는 메타데이터를 바꾼다 |
| 제거 | `DELETE /api/nodes/{id}` | 블록, known host, `servers.json` 항목을 지운다. 허브는 거부 |
| 다시 테스트 | `POST /api/nodes/{id}/probe` | 인벤토리 노드의 프로브를 다시 돌린다(추가: *Test again* 버튼용) |

그래서 `Host` 블록에 `UserKnownHostsFile ~/.ssh/guardian_known_hosts`와 `HostKeyAlias <id>`가 붙는다(2장).

프로브 결과: `ssh <id>`가 `Permission denied (publickey)`로 실패하면 노드는 등록되지만 `key not installed`로 보고된다.
UI는 노드의 `~/.ssh/authorized_keys`에 덧붙일 공개키 줄(또는 관리용 Mac에서 `scripts/setup-hub-ssh.sh <id>`)과 *Test again*
버튼을 보여준다. `arch`는 입력받지 않고 프로브에서 얻는다.

프로브 명령(`apps/agent/nodes_handler.go`의 `probeCommand`)은 고정되어 있다:

```
uname -m; if sudo -n true 2>/dev/null; then echo sudo=yes; else echo sudo=no; fi
```

`arch` 말고도 노드 로그인 사용자에게 비밀번호 없는 sudo가 있는지(`probe.sudo`, `yes` 또는 `no`)를 보고한다. 운영(ops) 프로젝트의
정리 액션이 journal, apt, 커널 작업에 `sudo -n`을 쓰기 때문이다([server-ops-project.md](server-ops-project.ko.md)). 대시보드는
이를 프로브 결과 옆에 칩으로 보인다.

UI(`#/nodes/new`, Nodes의 *Add node* 버튼에서): 연결 폼 -> *Check*가 확인할 호스트 키 지문을 보여준다 -> 메타데이터 폼 ->
*Register* -> 필요하면 키 안내와 함께 프로브 결과 -> 노드 페이지 링크. 노드 상세에는 *Edit connection*과 *Remove node*(id를
입력해 확인, 허브는 비활성)가 생긴다.
(2026-10-18: 마법사는 `#/fleet/nodes/new`에 있고, *Add node* 버튼은 Fleet의 노드 탭에, 노드 페이지는 `#/fleet/nodes/<id>`에
있다. 예전 `#/nodes/...` 해시도 열린다. 노드 상세에는 *Pin host key*와 *Revoke hub key*도 있고, *Remove node*는 먼저 키를
회수하거나 명시적으로 건너뛰기를 체크하라고 한다.)

## 5. 이전

0. `install-agent-service.sh --hub`는 `~/.guardian/data/servers.json`이 아직 없을 때만 저장소 사본으로 시드하고(대시보드 등록을
   덮어쓰지 않는다) `agent.env`에 `GUARDIAN_SERVERS_FILE`을 쓴다.
   (2026-10-18: 호출은 `install-agent-service.sh <hub-id>`이며 `--hub` 플래그는 없다. 시드 원본은 허브 자신의 인벤토리이고,
   `--offline`이면 로컬 `configs/servers.json` 스냅샷이다.)
1. `setup-hub-ssh.sh`는 `~/.ssh/guardian_fleet.conf`를 쓰고(예전 관리 블록과 노드별 내용이 같은 migrated 모양),
   `~/.ssh/config` 맨 위에 `Include ~/.ssh/guardian_fleet.conf`를 넣고, 예전 `# BEGIN guardian-hub-fleet` 블록을 지운다(처음
   바꿀 때 `~/.ssh/config.pre-guardian-fleet`을 남긴다). 여러 번 돌려도 같고, migrated 모양의 블록만 바꾼다: fleet 파일의
   대시보드 등록(pinned) 블록과 손으로 쓴 블록은 유지한다. `--revoke`(노드를 명시하지 않으면)는 자신이 쓴 migrated 블록과
   예전 블록을 지운다. Include 줄은 fleet 파일에 Host 블록이 하나도 남지 않을 때만 지운다(그때 파일도 지운다).
   `scripts/pull-inventory.sh`는 허브 인벤토리를 `configs/servers.json`(git이 무시하는 로컬 스냅샷)으로 다시 가져온다.
2. agent는 `/api/nodes`의 `HostName`/`User`/`Port`를 fleet 파일에 있으면 거기서 읽고(없으면 `servers.json` 필드로),
   그래서 이전되기 전까지 노드에는 아무것도 바뀌지 않는다.
3. 나중: 모든 스크립트가 fleet 파일을 읽게 되면 `servers.json`에서 연결 필드를 뺀다.

코드 변경의 일부로 허브에 배포되는 것은 없다. 배포는 별도의, 확인을 거친 단계다.

## 6. API 레퍼런스 (프런트엔드가 부르는 것)

모든 경로는 `/api` 아래이며, 나머지 API와 같은 bearer 토큰 또는 세션 쿠키 뒤에 있다. JSON으로 주고받는다(본문이 있는 요청은
`Content-Type: application/json`, 본문 한도 64 KB, 알 수 없는 요청 필드는 400으로 거부). 브라우저 클라이언트는 다른 호출과 마찬가지로
CORS 목록이 허용해야 한다(preflight에서 이제 `PUT`도 허용된다).

### 예약된 id

`new`, `registration`, `check`, `probe`는 노드 id가 될 수 없다(API 경로 조각이나 대시보드 라우트다): `check`, 등록, `PUT`, `DELETE`,
`probe`에서 `field: "id"`와 함께 `400 invalid_field`.

### 오류

모든 실패는 이 모양이다. `code`는 고정이고, `error`는 표시용이며, 실패가 요청 필드 하나에 관한 것이면 `field`가 그 필드를
가리키고(입력란 표시에 쓴다), `hint`는 선택적인 액션이며, 일부 코드는 키를 더 담는다.

```json
{"error": "hostname: must be an IPv4 address or a DNS name (letters, digits, dots and dashes)",
 "code": "invalid_field", "field": "hostname"}
```

| 상태 | `code` | 의미 |
|---|---|---|
| 400 | `invalid_json` | 본문이 올바른 JSON이 아니거나, 알 수 없는 필드나 뒤에 남은 데이터가 있다 |
| 400 | `invalid_field` | 검증 실패(`field`는 예를 들어 `id`, `hostname`, `user`, `port`, `host_key_fingerprint`, `metadata.tier`, `metadata.guardian_role`) |
| 400 | `fingerprint_required` | `host_key_fingerprint` 없이 등록하거나 hostname/port를 바꾸는 수정 |
| 403 | `hub_protected` | 허브 `DELETE` |
| 404 | `not_found` | 알 수 없는 노드 id |
| 409 | `include_missing` | `~/.ssh/config`가 첫 `Host`/`Match` 앞에서 fleet 파일을 포함하지 않는다. `hint`에 추가할 줄 |
| 409 | `inventory_not_configured` / `inventory_missing` | `GUARDIAN_SERVERS_FILE`이 설정되지 않았거나 파일이 없어서 인벤토리가 읽기 전용이다 |
| 409 | `id_exists` | id(또는 name)가 이미 `servers.json`이나 fleet 파일에 있다 |
| 409 | `fleet_busy` | 다른 프로세스(예: `setup-hub-ssh.sh`)가 10초 동안 fleet 잠금을 잡고 있었다. `hint`는 다시 시도하라고 한다 |
| 409 | `unmanaged_block` | fleet 파일에 이 별칭의 손으로 고친 또는 다른 모양의 `Host` 블록이 있다. agent는 건드리지 않는다 |
| 409 | `fingerprint_mismatch` | 노드가 지금 확인한 지문과 다른 키를 내보인다. 응답에 `keys`(`check`와 같은 모양)가 있다 |
| 500 | `verify_failed` | `ssh -G <id>`가 요청한 hostname/user/port로 풀리지 않았다. **모든 파일을 복원했다** |
| 500 | `write_failed` | 파일 쓰기가 실패했다. 파일을 복원했다 |
| 500 | `fleet_lock` | fleet 잠금 파일을 만들거나 열거나 잠그지 못했다 |
| 502 | `unreachable` / `scan_failed` | `ssh-keyscan`이 호스트 키를 받지 못했거나(`hint`에 stderr 끝부분) 실행되지 못했다 |

### GET /api/nodes/registration

```json
{
  "fleet_file": "/home/ubuntu/.ssh/guardian_fleet.conf",
  "known_hosts_file": "/home/ubuntu/.ssh/guardian_known_hosts",
  "ssh_config_file": "/home/ubuntu/.ssh/config",
  "include_line": "Include ~/.ssh/guardian_fleet.conf",
  "include_present": true,
  "include_effective": true,
  "inventory_file": "/home/ubuntu/.guardian/data/servers.json",
  "inventory_writable": true,
  "hub_tailscale_ip": "100.64.0.7",
  "fleet_public_key": "from=\"100.64.0.7\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nza... guardian-hub-fleet@site-a-arm1",
  "managed_nodes": ["oci-a", "edge-1"],
  "unmanaged_hosts": ["bastion"],
  "blockers": [],
  "can_register": true
}
```

`include_present`는 어떤 `Include`가 fleet 파일을 가리킨다는 뜻이고, `include_effective`는 그것이 첫 `Host`/`Match` 줄보다 앞에
있다는 뜻이다(그렇지 않으면 모든 `Host` 블록에 적용되지 않는다). `blockers`는 지금 등록을 막는 것(Include 줄 없음, 읽기 전용 인벤토리)을
평범한 문장으로 나열하고, `can_register`는 `blockers`가 비었는지다. `validation`(위 예에는 없음)은 8장 규칙의 기계용 사본
(`nodeFormRules`: 패턴, 한도, 예약 id)이며, 대시보드 폼이 허브 에이전트와 같은 검사를 하게 한다. 최종 판단은 허브 에이전트가 한다. 허브의 공개키
파일이 없거나 허브에 `tailscale_ip`가 없으면 `fleet_public_key`는 비고(제한 없는 줄을 주지 않는다) `fleet_public_key_blocker`(그리고
`fleet_public_key_note`)가 이유를 말한다. 그때 프로브 응답은 `authorized_key` 대신 `authorized_key_blocker`를 담는다.
`unmanaged_hosts`는 agent가 건드리지 않는, 손으로 쓴 블록의 `Host` 패턴이다.

### POST /api/nodes/check

요청 `{"id": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22}`(`port` 기본값 22).
검증한 뒤 `ssh-keyscan -T 5 -p <port> <hostname>`. 아무것도 쓰지 않는다. `id_exists`는 정보일 뿐이다
(기존 노드의 hostname/port를 바꾸기 전에도 같은 호출을 쓴다).

```json
{
  "ok": true, "id": "edge-1", "hostname": "203.0.113.9", "port": 22, "id_exists": false,
  "keys": [
    {"type": "ssh-ed25519", "fingerprint": "SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk"},
    {"type": "ecdsa-sha2-nistp256", "fingerprint": "SHA256:L+5UBANOVSvPSFxRbAg3IvTiKCDBqHV66Pu4fG4X/D0"}
  ]
}
```

운영자는 지문 하나를 확인한다(노드에서 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`와 비교). 그 문자열 그대로
`host_key_fingerprint`로 보낸다.

### POST /api/nodes -> 201

요청:

```json
{
  "id": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22,
  "host_key_fingerprint": "SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk",
  "metadata": {
    "tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node",
    "cli_support": ["ssh_agent_remote_exec"], "tailscale_tags": ["tag:fleet"],
    "description": "Edge box"
  }
}
```

`metadata`와 그 안의 각 키는 선택 사항이다(`guardian_role` 기본값 `node`, `hub`는 거부).
허용 값: `tier`는 글자·숫자·공백·`._:-`로 1~32자. `role`, `guardian_role`, `cli_support` 항목은 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`.
`tailscale_tags` 항목은 `tag:<소문자, 숫자, 대시>`. 목록은 최대 16개. `description`은 최대 500자, 줄바꿈 없음. `name`은 id로
정해지고, `arch`는 입력할 수 없다(프로브에서 온다). 허브 에이전트는 노드를 다시 스캔해 지문이 내보이는 키 중 하나와 맞는지 요구한다. 응답:

```json
{
  "ok": true,
  "node": {
    "id": "edge-1", "name": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22,
    "arch": "aarch64", "group": "", "key": "", "recommended": false, "description": "Edge box",
    "tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node",
    "cli_support": ["ssh_agent_remote_exec"], "tailscale_tags": ["tag:fleet"],
    "connection": "fleet_file", "host_key_mode": "pinned"
  },
  "probe": {"status": "ok", "reachability": "ok", "arch": "aarch64", "sudo": "yes", "duration_ms": 412}
}
```

`probe.status`는 `ok`, `key_not_installed`(`Permission denied`) 또는 `unreachable`(그 밖의 모든 실패)이다. `ok`가 아니면 `stderr`
(마지막 400자, 한 줄)가 있다. `probe.reachability`는 fleet 진단, 명령 실행, SSH 감사도 쓰는 공통 ssh 분류다(`ok`, `auth_failed`,
`host_key_changed`, `unreachable`, `command_failed`. `apps/agent/ssh_classify.go`). `probe.sudo`는 프로브가 성공했을 때 `yes` 또는
`no`(로그인 사용자의 비밀번호 없는 sudo)이고, 그렇지 않으면 없다. `key_not_installed`이면 응답에
`"authorized_key": "from=\"100.64.0.7\",no-agent-forwarding,... ssh-ed25519 AAAA... guardian-hub-fleet@..."`도 있다
(노드의 `~/.ssh/authorized_keys`에 덧붙일 줄). 프로브가 무엇을 말하든 노드는 등록된 채로 남는다. `warnings`(문자열 목록)는
`arch` 저장이 실패했을 때만 나온다.

### PUT /api/nodes/{id} -> 200

모든 필드는 선택 사항이다. 본문은 현재 값에 합쳐진다.

```json
{
  "hostname": "198.51.100.4", "user": "deploy", "port": 2222,
  "host_key_fingerprint": "SHA256:...",
  "metadata": {"tier": "Tier-1", "description": null, "cli_support": ["claude"]}
}
```

- 들어 있는 메타데이터 키는 값을 바꾼다. `null`(또는 `""`/`[]`)은 인벤토리 항목에서 그 키를 지운다. 없는 키는 유지한다.
  허브의 `guardian_role`은 바꾸거나 지울 수 없고, 다른 노드는 `hub`가 될 수 없다(400, `field: "metadata.guardian_role"`).
- **호스트 키 다시 확인(노드 고정):** `{"host_key_fingerprint": "SHA256:..."}`만 담은 `PUT /api/nodes/{id}`(노드의 현재 주소에 대한
  `check`에서 얻은 값). 허브 에이전트는 다시 스캔해 지문이 맞는지 요구하고, 그 키를 `guardian_known_hosts`에 `HostKeyAlias`로 저장하고,
  노드 블록을 registered(pinned) 모양으로 다시 쓴 뒤 `ssh -G`로 확인하고(맞지 않으면 롤백) 프로브한다. (a) migrated `accept-new`
  블록(hostname, user, port는 그대로)과 (b) fleet 블록이 없는 `inventory` 노드 모두에 쓸 수 있다: 블록은 요청에 `hostname`/`user`/`port`가
  있으면 그것으로, 없으면 인벤토리 항목으로 만든다(`tailscale_ip`가 `100.64.`로 시작하면 그 값, 아니면 `hostname`. `user` 기본값
  `ubuntu`, `port` 22). 다른 입력과 똑같이 검증한다(쓸 값이 없으면 `field: "hostname"`과 함께 400). 이미 고정된 노드에 보내면 키를
  바꾼다(호스트 키 교체). unmanaged 블록은 언제나처럼 409로 거부한다. 응답:

  ```json
  {"ok": true,
   "node": {"id": "oci-a", "hostname": "100.64.0.5", "user": "ubuntu", "port": 22,
            "connection": "fleet_file", "host_key_mode": "pinned", "...": "..."},
   "probe": {"status": "ok", "reachability": "ok", "arch": "aarch64", "sudo": "yes", "duration_ms": 380}}
  ```

- `hostname`이나 `port`를 바꾸려면(또는 fleet 블록이 아직 없는 노드에 첫 연결을 주려면) 새 목적지에 대한 새 `check`의
  `host_key_fingerprint`가 필요하다. 그 id의 고정 키는 바뀐다. `user`만 바꾸거나 같은 값을 보내면 필요 없다(migrated 블록은 모양과
  신뢰 방식을 유지한다).
- fleet 블록이 unmanaged인 노드의 연결 수정은 거부한다(`unmanaged_block`). 메타데이터 수정은 `servers.json`만 건드리므로 그대로 된다.

응답: `{"ok": true, "node": {...}}`, 연결을 썼으면 `"probe": {...}`(위와 같은 모양, 키가 없으면 `authorized_key`)도 붙는다.

### DELETE /api/nodes/{id} -> 200

```json
{"ok": true, "id": "edge-1", "removed": {"fleet_block": true, "known_hosts": true, "inventory": true}}
```

허브는 거부한다(403). `servers.json`에만 있는 노드는 그 항목만 사라진다(`fleet_block: false`). unmanaged 블록은 절대 지우지 않는다(409).

삭제 절차: 먼저 허브 키를 회수하고, 그다음 삭제한다. 삭제는 허브 자신의 기록만 지운다. 허브 공개키는 회수하기 전까지 노드의
`~/.ssh/authorized_keys`에 남는다. 회수에는 삭제가 없애는 fleet SSH 경로가 필요하므로 순서는 회수, 그다음 삭제다.

### GET /api/nodes/{id}/revoke-key -> 200

노드에 접속하지 않는 미리보기(알 수 없는 노드 404, 허브 공개키가 없거나 쓸 수 없으면 409 `hub_key_missing` / `hub_key_invalid`):

```json
{"node": "edge-1", "target": "ubuntu@203.0.113.9[:port]", "fingerprint": "SHA256:...",
 "match": "ssh-ed25519 AAAA...", "note": "..."}
```

`match`는 허브 fleet 공개키(`GUARDIAN_FLEET_PUBKEY_FILE`, 설치 키 줄을 만드는 바로 그 파일)의 `<type> <base64>`다. `port`는 22가
아닐 때만 보인다.

### POST /api/nodes/{id}/revoke-key -> 200

본문 `{"confirm_id": "<id>"}`. 노드 id와 같아야 한다(아니면 400 `confirm_mismatch`, ssh를 시작하지 않는다). fleet 잠금 아래에서
agent는 fleet 키로 접속하고(프로브와 같은 ssh 경로와 호스트 키 정책) 노드의 `~/.ssh/authorized_keys`에서 정확한 `match` 텍스트를
담은 줄만 지우는 고정 스크립트를 돌린다. 키는 `[a-z0-9@.-]` 타입과 base64 본문으로 검증한 뒤 작은따옴표 상수에 넣는다. 자유 텍스트는
셸에 닿지 않는다. 스크립트는 파일 옆에 백업 `authorized_keys.guardian-bak-<timestamp>`를 남기고, 소유자와 권한을 유지한 사본을
쓴 뒤 `mv`로 제자리에 놓는다.

```json
{"ok": true, "removed": 1, "hub_key_revoked_at": "2026-10-12T09:30:00Z",
 "note": "After this the hub can no longer reach the node over SSH; the next step is to delete the node."}
```

성공하면(`removed`가 0, 즉 키가 설치되어 있지 않을 때도) agent는 노드의 인벤토리 항목에 `hub_key_revoked_at`(RFC3339, UTC)을
저장한다. 그 쓰기가 실패하면 응답에 대신 `warning`이 있다. 이 필드는 `GET /api/nodes`의 모든 노드와 등록/수정의 `node` 객체에도
문자열로, 허브 키가 아직 설치되어 있다고 보는 동안에는 `null`로 나온다. 대시보드는 이를 써서 Remove 전에 먼저 회수하라고(또는
명시적으로 건너뛰라고) 요구한다. `DELETE` 자체는 바뀌지 않았고 이를 확인하지 않는다.

허브 자신의 항목은 GET과 POST 모두 거부한다: 400
`{"error": "refusing to revoke the hub's key on the hub itself", "code": "hub_protected"}`.

모든 POST는 감사 로그에 기록된다(경로 `/api/nodes/{id}/revoke-key`, `targets: [id]`, `status` `ok` 또는 `error`, `detail`
`revoke hub key: removed=<n>` 또는 실패 내용).

맞는 줄이 없으면: `removed: 0`, 바뀐 것 없음, note가 그렇게 말한다. ssh 연결 불가나 실패:
502 `{"error": "...", "reachability": "unreachable|auth_failed|host_key_changed|command_failed"}`.

### POST /api/nodes/{id}/probe -> 200

본문 없음. 인벤토리의 어떤 노드든:
`{"ok": true, "id": "edge-1", "probe": {"status": "key_not_installed", "reachability": "auth_failed", "stderr": "...", "duration_ms": 90}, "authorized_key": "..."}`.
프로브가 성공하면 인벤토리 항목에 `arch`를 저장한다.

### GET /api/nodes (바뀜)

각 노드에 다음이 더 붙는다:

- `connection`: 노드가 fleet 파일에 관리 블록을 가지면 `"fleet_file"`(그때 `hostname`, `user`, `port`는 fleet 파일의 값),
  아니면 `"inventory"`(`servers.json`의 값).
- `host_key_mode`(fleet 파일 노드만): `"pinned"`(대시보드에서 등록) 또는 `"accept-new"`(migrated, 처음 접속 때 신뢰).
  UI는 후자에 *Re-confirm host key*를 제공할 수 있다.

## 7. 알아 둘 동작

- **순서와 롤백.** 쓰기는 fleet 잠금(프로세스 뮤텍스와 `<fleet file>.lock`에 대한 `flock`, `setup-hub-ssh.sh`와 공유. 10초를
  기다려도 못 잡으면 `fleet_busy`로 실패) 아래에서 일어난다: 고정 키, fleet 블록, 인벤토리 항목(각각 원자적: 같은 디렉터리의
  임시 파일, 권한 600, rename. 이전 내용은 `<file>.bak`으로 남는다). 그다음 `ssh -G -- <id>`가 요청한 `hostname`, `user`, `port`
  (registered 모양이면 `hostkeyalias`도)를 보고해야 한다. 아니면 모든 파일을 이전 내용 그대로 바이트 단위로 복원하고(없던 파일은
  지우고) `verify_failed`로 실패한다. 프로브는 잠금을 푼 뒤에 돌며 아무것도 롤백하지 않는다.
- **허브 에이전트가 쓰는 곳.** `GUARDIAN_SSH_FLEET_FILE`(기본 `~/.ssh/guardian_fleet.conf`), `GUARDIAN_SSH_KNOWN_HOSTS`(기본
  `~/.ssh/guardian_known_hosts`), `GUARDIAN_SSH_CONFIG`(기본 `~/.ssh/config`, 읽기만), `GUARDIAN_FLEET_PUBKEY_FILE`(기본
  `~/.ssh/guardian_fleet.pub`), 그리고 `GUARDIAN_SERVERS_FILE`. 경로 재정의는 테스트용이며, 블록 텍스트는 언제나
  `~/.ssh/guardian_known_hosts`라고 쓴다. 등록은 `GUARDIAN_SERVERS_FILE`이 설정되지 않으면 인벤토리 쓰기를 거부하므로, agent가
  git 체크아웃 안의 `configs/servers.json`으로 대체해서 고치는 일은 생길 수 없다.
- **인벤토리 수정은 나머지를 그대로 둔다.** 바뀌지 않은 항목은 바이트 그대로 다시 쓴다(파일의 두 칸 들여쓰기는 예외). 고친 항목은
  키 순서와 알 수 없는 필드(`recommended`, `backup_replica`, `group`, ...)를 유지한다. 새 항목은 `id`, `name`, 메타데이터, `arch`를
  갖고 연결 필드는 없다: 연결 필드의 유일한 출처는 fleet 파일이다.
- **명령 허용 목록.** `/api/exec/remote`의 `target_host`는 인벤토리 **id**만 맞춘다(ssh는 id를 `Host` 별칭으로 쓴다). name이나
  hostname은 400으로 거부하며, 어떤 노드와 맞으면 오류가 그 노드의 id를 알려준다(`... is not an id; use the node id "legacy-1"`).
  fleet 파일에만 있는 hostname도 대상이 아니다. `/api/fleet/exec`와 그 스트림의 `targets`는 예전처럼 id 또는 name을 맞춘다.
- **허브 sudo가 꺼져 있는 동안 허브는 fleet 대상이 아니다.** 허브는 fleet 목록에 있고, 허브로의 ssh는 agent 샌드박스 밖에서 명령을
  돌리게 된다(hub-sudo 우회). 설치에 허브 sudo가 없으면 `/api/exec/remote`, `/api/fleet/exec`, 스트림에서 허브를 명시한 대상(id
  또는 name)은 409 `{"error": "the hub is not a fleet target while hub sudo is off; use /api/exec"}`로 거부하고, 대상 없는 fleet 실행은
  허브를 그냥 뺀다. "허브 sudo 꺼짐"은 agent 자신의 샌드박스에서 읽는다: `/proc/self/status`의 `NoNewPrivs: 1`(기본 유닛이
  `NoNewPrivileges=yes`를 설정하고, drop-in `hub-sudo.conf`가 `no`로 바꾼다). `GUARDIAN_HUB_SUDO=on|off`가 이를 재정의한다.
  `/proc`이 없으면 아무것도 거부하지 않는다.
- **감사 로그.** `<data>/audit/exec.jsonl`은 운영 로그이며 위변조 방지가 아니다(agent 사용자와 그 명령이 다시 쓸 수 있다).
  10 MB에서 회전하고(`exec.jsonl.1` .. `.5`), `GET /api/audit?limit=`가 파일들을 걸쳐 읽는다. 실행한 명령 말고도 노드 쓰기
  (`POST /api/nodes`, `PUT|DELETE /api/nodes/{id}`: 빈 `command`, `detail` = 작업 요약, `status` `ok`/`error`), 키 회수, 거부된 실행
  요청(`status: "rejected"`, `detail` = 이유)을 기록한다. `actor`는 `session@<ip>`(대시보드 쿠키) 또는 `token@<ip>`(Bearer
  `AGENT_TOKEN`)다.
- **대상별 호스트 키 정책.** agent가 시작하는 모든 ssh(명령 실행, fleet 진단, 실행 스트림, SSH 감사, 등록 프로브)는 `executeSSH`를
  거친다. fleet 블록이 registered 모양인 노드(`host_key_mode` `pinned`)에는 `StrictHostKeyChecking` 옵션을 넘기지 않으므로 블록의
  `yes`, `HostKeyAlias`, `UserKnownHostsFile`이 적용된다: 고정 키가 없거나 바뀌면 실패한다. 손으로 쓴 블록도 마찬가지로 그 블록의
  정책을 따른다. 블록이 없는 노드와 migrated 블록(고정 키 없음)만 `-o StrictHostKeyChecking=accept-new`를 받는다. fleet 파일이
  있지만 읽을 수 없으면 `executeSSH`는 닫힌 쪽으로 실패한다(exit 255, ssh를 시작하지 않음). 파일이 없으면 블록이 없다는 뜻이다.
  별칭 앞의 `--`와 `safeSSHHost` 검사는 그대로이고, `ssh -G`는 영향을 받지 않는다.
- **샌드박스.** `server-guardian-agent.service`는 바꿀 필요가 없다: 홈은 쓸 수 있고(`ProtectSystem=full`은 /usr, /boot, /etc만
  보호한다), `InaccessiblePaths`는 `~/.ssh/guardian_fleet`(예전 개인키 경로)을 숨기지만 `guardian_fleet.conf`,
  `guardian_known_hosts`, `~/.guardian/data`는 숨기지 않으며, `ssh`/`ssh-keyscan`은 예전과 같은 샌드박스 안에서 돈다.

## 8. 검증 레퍼런스

`id` `^[a-z0-9][a-z0-9-]{0,62}$`이고 위의 예약어가 아님. `hostname`은 3장의 정규 IPv4 또는 DNS 이름(추가로 마지막 레이블이 모두
숫자인 이름은 거부, 전체 길이 최대 253). `user` `^[a-z_][a-z0-9_-]{0,31}$`. `port` 1-65535(JSON 숫자). `host_key_fingerprint`
`^SHA256:[A-Za-z0-9+/]{43}$`. 요청 값은 검증 전에 `ssh`/`ssh-keyscan`에 닿지 않고, `ssh`는 언제나 별칭 앞에 `--`를 받으며,
`ssh-keyscan`(`--`가 없음)은 `-`로 시작할 수 없는 hostname만 받는다.

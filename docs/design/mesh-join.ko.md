# 메시 가입: 허브에서 tailnet 노드 승인과 등록 해제

[English](mesh-join.md) | 한국어

상태: 구현됨 (2026-10-18). 관련 문서: [headscale 운영](../operations/headscale.ko.md),
[노드 등록](node-registration.ko.md), [server-ops-project.ko.md](server-ops-project.ko.md)의 "P7".

## 1. 목표

새 노드가 인증 키 없이 플릿 tailnet에 가입한다. 새 노드에서 사람이 `tailscale up --login-server <url>`을 실행하면 등록
URL이 나온다. 운영자가 그 URL을 대시보드에 붙여 넣으면, 허브가 headscale에 노드를 등록하고 태그를 붙인다. 그다음 기존
노드 등록으로 Guardian에 노드를 추가한다.

이전에는 관리용 Mac(`~/.config/headscale/.env`)과 자격 증명 번들에 둔, 여러 번 쓸 수 있고 수명이 긴 인증 키로 가입했다.
재사용 키가 새면 누구나 tailnet에 기기를 넣을 수 있다. 이 흐름에는 샐 키가 없다.

## 2. 허브가 가입 전체를 할 수 없는 이유

허브는 tailnet으로만 노드에 닿는다. 허브의 fleet 키는 허브의 Tailscale IP에서만 받아들여진다. 새 노드는 아직
tailnet에 없으므로 허브가 그곳에서 `tailscale up`을 실행할 수 없다. 새 노드에서의 명령 한 줄은 사람이 한다.

## 3. 흐름

1. 새 노드에서: `sudo tailscale up --login-server https://<headscale> --hostname <name>`. 명령이 기다리면서
   `https://<headscale>/register/hskey-authreq-<24자>`를 출력한다.
2. 대시보드(플릿 > 노드 > 노드 추가, "메시 가입 승인" 패널)에서: URL을 붙여 넣고, 태그와 (선택) 노드 이름을 고른 뒤
   승인한다.
3. 허브가 fleet SSH로 제어 서버에서 고정된 스크립트 하나를 실행한다:
   `sudo -n headscale auth register --auth-id <id> --user <user> -o json`, 이어서
   `headscale nodes tag -i <node id> -t <tag> ...`, 이름을 줬으면 `headscale nodes rename`. 노드의 이름, 주소, 태그를
   다시 읽는다(제어 서버에서 요약하므로 노드 키는 그곳에 남는다).
4. 새 노드의 `tailscale up`이 끝난다. 패널의 "이 노드를 Guardian에 등록"을 누르면 노드 추가 마법사에 노드 이름과
   tailnet IPv4가 채워진다.

## 4. API

`GET /api/mesh/join`: `{enabled, problem?, host?, user?, tags?, ready?, server_url?}`. 켜져 있으면 허브가 제어 서버의
`/etc/headscale/config.yaml`에서 `server_url`을 읽는다. 이 호출로 연결 가능 여부와 비밀번호 없는 sudo도 확인한다
(`ready`).

`POST /api/mesh/join`, 본문 `{"auth": "<URL 또는 id>", "tags": ["tag:fleet"], "name": "edge-7"}`:

- `auth`에는 `hskey-authreq-[A-Za-z0-9_-]{24}`(headscale v0.29의 AuthID)에 맞는 id가 정확히 하나 있어야 한다.
- 모든 태그는 `GUARDIAN_HEADSCALE_TAGS`에 있어야 하고, 하나 이상 필요하다(노드는 태그 기기로 가입한다).
- `name`은 선택이며 노드 id 규칙 `^[a-z0-9][a-z0-9-]{0,62}$`를 따른다.
- 모르는 필드는 거부한다(요청마다 사용자를 고를 수 없다).

응답: `200 {ok, node_id, name, ipv4, ipv6, tags, user, host, warning?}`; 잘못된 입력은 `field`가 붙은 `400`; 설정이
없으면 `409 mesh_join_off`; headscale 오류는 `502 mesh_join_failed`. 만료되었거나 이미 쓴 로그인은 "registration
cache" 오류가 나며, 응답이 운영자에게 `tailscale up`을 다시 실행하라고 알린다. 가입은 한 번에 하나씩 실행한다. 모든
요청은 audit 로그에 남는다(`route /api/mesh/join`, 대상은 제어 서버, detail에 노드 id, 이름, IPv4, 태그 또는 오류).

## 5. 설정 (허브 `~/.guardian/agent.env`)

| 키 | 의미 |
|---|---|
| `GUARDIAN_HEADSCALE_HOST` | 제어 서버의 inventory id. 비우면 기능이 꺼진다 |
| `GUARDIAN_HEADSCALE_USER` | 새 노드를 등록할 headscale 사용자 |
| `GUARDIAN_HEADSCALE_TAGS` | 대시보드가 줄 수 있는 태그, 쉼표로 구분 (기본 `tag:fleet`) |

설치 스크립트는 재설치 때 이 줄을 유지한다. 실제 호스트와 사용자 이름은 허브에만 있고 git에는 들어가지 않는다.

## 6. 안전

- Guardian이 headscale에 쓰는 것은 이것뿐이다. 재시작, 설정, ACL, 노드 삭제, 업그레이드는 사람이 한다
  (`headscale*`는 `ops:restart_service`의 보호 유닛이다).
- 스크립트는 고정된 텍스트다. 끼워 넣는 값은 모두 따옴표, 공백, 셸 메타문자가 없는 패턴에 맞아야 한다. 태그는 허브의
  목록에서, 사용자는 허브의 환경에서 온다.
- 로그인을 승인하면 그 로그인을 시작한 기기를 믿게 된다. 운영자가 새 노드에 출력된 것을 직접 본 URL만 승인한다.
  다른 곳에서 온 URL을 승인하면 남의 기기가 고른 태그를 달고 tailnet에 들어온다.
- 태그는 ACL 권한을 준다(`/etc/headscale/policy.hujson`의 `tagOwners`). `GUARDIAN_HEADSCALE_TAGS`에는 노드에 필요한
  태그만 둔다.
- 다른 쓰기와 마찬가지로 대시보드는 운영자의 세션이나 `AGENT_TOKEN`이 있어야 한다.

## 7. 재사용 인증 키 만료 (2026-10-18)

새로 백업한 뒤 제어 서버의 pre-auth 키를 모두 `headscale preauthkeys expire`로 만료시켰다. 아직 유효했던 재사용 키는
다섯 개였다(태그 `tag:mesh-ctl`, `tag:fleet`과 `tag:sw-ctl`·`tag:af-core`·`tag:gitea` 조합, 관리용 기기를 위한 태그
없는 키 하나). 이 키를 쓰는 자동화는 없었다(저장소, 허브, 제어 서버, 관리용 Mac을 찾아봄). 관리용 Mac에는
`~/.config/headscale/.env`가 이미 없었다. 등록된 노드에는 영향이 없다: 28대 모두 온라인을 유지했다.
`scripts/export-credentials.sh`는 더 이상 `headscale.env`를 번들에 넣지 않고, `scripts/import-credentials.sh`는 그것을
복원하지 않으며 키 없는 로그인 방법을 출력한다. 관리용 기기(사람의 기기, 태그 없음)는 `tailscale login
--login-server ...`로 시작하고 운영자가 제어 서버에서 `headscale auth register`를 직접 실행해 가입한다. 대시보드는
태그가 붙는 노드만 승인한다.

## 8. 노드 등록 해제

기능이 켜져 있으면 플릿 > 노드에 "tailnet 노드" 패널이 나온다. headscale의 모든 노드를 IPv4, 태그, 온라인 상태, 같은
tailnet IP를 가진 Guardian 노드와 함께 보여 준다.

- `GET /api/mesh/nodes`: `{host, tags, nodes: [{id, name, ipv4, ipv6, tags, user, online, last_seen,
  inventory_id?, protected?}]}`. 목록은 제어 서버에서 요약한다(노드 키, pre-auth 키 없음).
- `POST /api/mesh/nodes/{id}/remove`, 본문 `{"confirm_name": "<노드 이름>"}`이 `headscale nodes delete -i <id>
  --force`를 실행한다. 허브는 먼저 목록을 다시 읽고 그 결과로 판단한다.
- 거부(`409 mesh_node_protected`): 허브(inventory의 `tailscale_ip`), 제어 서버(`GUARDIAN_HEADSCALE_HOST` 이름이나 그
  IP), `GUARDIAN_HEADSCALE_TAGS`의 태그가 하나도 없는 노드(사람의 기기와 다른 역할은 제어 서버에서 직접 지운다). 이름이
  틀리면 `400 confirm_mismatch`, 모르는 id는 `404`.
- Guardian 노드이기도 한 노드는 거부하지 않는다. 죽은 노드도 지울 수 있어야 하기 때문이다. 대신 대화상자가 경고한다:
  해제하면 허브가 tailnet으로 그 노드에 닿지 못하므로, 허브 키 회수와 Guardian 노드 삭제를 먼저 한다.
- 모든 요청은 audit 로그에 남는다(`route /api/mesh/nodes/{id}/remove`).

## 9. 하지 않은 것

- 대시보드에서 노드 만료(삭제 없이 로그아웃)와 이름 변경.
- 대시보드에서 pre-auth 키 만들기: 노드에는 필요 없고, 일부러 피했다.

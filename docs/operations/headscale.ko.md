# headscale 운영

[English](headscale.md) | 한국어

headscale은 플릿 tailnet의 제어 서버다. `site-y-ec2`에서 돌며 `https://hs.example.com`를 제공한다. 허브는 모든
플릿 노드에 이 tailnet으로 닿는다. 허브의 fleet 키는 허브의 Tailscale IP에서만 받아들여진다(`authorized_keys`의
`from="..."`). 클라이언트가 마지막 네트워크 맵을 유지하므로, headscale이 잠깐 멈춰도 기존 연결은 유지된다. 오래 멈추면
새 노드가 가입할 수 없고, 키가 갱신되지 않으며, 변경 사항이 노드에 전달되지 않는다.

## 구성

| 항목 | 위치 |
|---|---|
| 서비스 | `headscale.service` (systemd), 실행 파일 `headscale` |
| 설정 | `/etc/headscale/config.yaml`, ACL `/etc/headscale/policy.hujson` (`policy.mode: file`) |
| 상태 | `/var/lib/headscale/db.sqlite` (SQLite, WAL), `noise_private.key`, 인증서 캐시 `cache/` |
| TLS | Let's Encrypt, `:443`에서 TLS-ALPN-01 |
| 로컬 전용 | 지표 `127.0.0.1:9090`, gRPC `127.0.0.1:50443` |
| 백업 | `headscale-backup.timer`(매일)가 `/usr/local/sbin/headscale-backup.sh`를 실행한다: DB를 실행 중에 `sqlite3 .backup`으로 뜨고 `/etc/headscale`과 `/var/lib/headscale`의 나머지를 더해 `/var/backups/headscale/headscale-YYYYMMDD.tar.gz`로 만든다. 최근 14개를 남긴다 |

백업은 같은 노드에 남는다. 노드 디스크를 잃으면 백업도 함께 잃는다. 노드 밖으로 복사하는 일은 아직 남아 있다.

## Guardian이 지켜보는 것

`fleet` 프로젝트의 야간 ops 점검(`policy.watch.headscale: true`, 02:30 UTC)은 모든 대상에서 읽기 전용 점검
`headscale_status`를 실행하고, 다음 중 하나라도 해당하면 ops 토론을 하나 연다.

- 서비스가 `active`가 아니다.
- TLS 인증서가 21일 안에 만료된다.
- `headscale*` 백업 타이머가 실패했거나, 마지막 실행이 48시간보다 오래됐다.
- 태그가 붙은 노드(사람의 기기가 아닌 플릿 노드)가 60분 넘게 오프라인이다.

어느 ops 토론의 에이전트든 `headscale_status`를 직접 실행할 수 있다. 토론은 headscale을 바꾸지 않는다:
`ops:restart_service`는 `headscale*`를 거부하고, 설정, ACL, 노드를 고치는 카탈로그 액션은 없다. 쓰기는
운영자의 메시 가입 승인과 등록 해제뿐이다(아래). 설계 메모:
[server-ops-project.ko.md](../design/server-ops-project.ko.md)의 "P7 headscale 제어 서버".

## 비상 접속

tailnet이 멈추면 허브는 `site-y-ec2`를 포함해 어떤 노드에도 닿지 못한다. 이때는 관리용 Mac을 쓴다. 이 노드에 쓰는
Mac의 키에는 `from=` 제한이 없고, 노드의 SSH(`:22`)는 공인 이름으로 닿는다.

```bash
ssh site-y-ec2
```

```bash
systemctl status headscale --no-pager
```

```bash
sudo journalctl -u headscale -n 200 --no-pager
```

노드의 백업으로 복원한다(먼저 서비스를 멈춘다. 압축 파일에는 `var-lib-headscale/`과 `etc-headscale/`이 들어 있다).

```bash
sudo systemctl stop headscale
sudo mkdir /tmp/hs-restore && sudo tar -xzf /var/backups/headscale/headscale-YYYYMMDD.tar.gz -C /tmp/hs-restore
sudo cp -a /tmp/hs-restore/var-lib-headscale/. /var/lib/headscale/
sudo cp -a /tmp/hs-restore/etc-headscale/. /etc/headscale/
sudo rm -f /var/lib/headscale/db.sqlite-wal /var/lib/headscale/db.sqlite-shm
sudo chown -R headscale:headscale /var/lib/headscale
sudo systemctl start headscale
```

## 노드 추가

새 노드는 두 단계로 들어온다. 둘 다 대시보드의 노드 추가 페이지에서 시작한다
([mesh-join.ko.md](../design/mesh-join.ko.md)).

1. **tailnet 가입.** 새 노드에서 사람이 "메시 가입 승인" 패널이 보여 주는 명령을 실행한다.

   ```bash
   sudo tailscale up --login-server https://hs.example.com --hostname <name>
   ```

   명령이 등록 URL(`.../register/hskey-authreq-...`)을 출력한다. 그 URL을 패널에 붙여 넣고 태그를 골라 승인하면,
   허브가 `site-y-ec2`에서 `headscale auth register`와 `headscale nodes tag`를 실행한다. 인증 키는 쓰지 않는다.
   허브의 `~/.guardian/agent.env`에 `GUARDIAN_HEADSCALE_HOST`와 `GUARDIAN_HEADSCALE_USER`가 있어야 한다.

2. **Guardian에 등록.** "이 노드를 Guardian에 등록"을 누르면 노드 추가 마법사에 노드 이름과 tailnet IP가 채워진다.
   마법사(`POST /api/nodes`, [node-registration.ko.md](../design/node-registration.ko.md))가 inventory 항목을 만들고
   호스트 키를 고정한다.

유효한 인증 키는 없다: 2026-10-18에 모든 pre-auth 키를 만료시켰다. 관리용 기기(사람의 기기, 태그 없음)는
`tailscale login --login-server https://hs.example.com`를 실행하고, 운영자가 출력된 id를 제어 서버에서 승인한다:
`sudo headscale auth register --user <user> --auth-id hskey-authreq-...`.

## 노드 등록 해제

플릿 > 노드의 "tailnet 노드" 패널이 headscale의 모든 노드를 보여 준다. "등록 해제"는 운영자가 이름을 입력한 뒤 태그가
붙은 노드를 headscale에서 삭제한다. 허브, 제어 서버, 태그 없는 기기는 여기서 해제할 수 없다. Guardian 노드이기도 한
tailnet 노드는 먼저 Guardian에서 허브 키를 회수하고 노드를 삭제한다. 해제한 뒤에는 허브가 tailnet으로 그 노드에 닿지 못한다.

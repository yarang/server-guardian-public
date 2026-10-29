# 2026-10-05 Fleet sshd 하드닝 (MaxAuthTries, PermitRootLogin)

[English](2026-10-05-fleet-sshd-hardening.md) | 한국어

> 2026-10-05 시점의 기록이며, 이후 코드가 바뀌었다.

| 항목 | 내용 |
|---|---|
| 일시 | 2026-10-05 12:50–14:30 UTC |
| 대상 | fleet 노드 23대 (servers.json 24대 중 edge-mini 제외) |
| 실행 경로 | 허브 site-a-arm1의 `POST /api/fleet/exec` (`targets` 지정), fleet SSH 키 사용 |
| 근거 | 노드별 SSH 감사 `GET /api/fleet/ssh` 결과 (SEC-SSH-004 실패 23대, SEC-SSH-001 경고 23대) |
| 승인 | 운영자(owner)가 대화에서 직접 승인. 토론·액션 승인 흐름(decision DB)은 거치지 않음 |
| 결과 | fleet 평균 SSH 점수 75 → 95, 실패·경고 0 |

## 배경

노드별 SSH 감사 기능(`879e881`, `c64a604`)을 허브에 배포한 뒤 처음으로 fleet 전체를 감사했다.
23대 모두 `sshd -T (sudo)`로 실제 적용 설정을 읽었고(`ok`), 결과가 같았다.

| 규칙 | 변경 전 | 변경 후 |
|---|---|---|
| SEC-SSH-001 root 로그인 금지 | 경고 23 (`without-password` 22, `prohibit-password` 1) | 통과 23 |
| SEC-SSH-002 키 인증만 허용 | 통과 23 | 통과 23 |
| SEC-SSH-003 공개키 인증 | 통과 23 | 통과 23 |
| SEC-SSH-004 MaxAuthTries ≤ 4 | 실패 23 (값 6) | 통과 23 (값 4) |
| SEC-SSH-005 비표준 포트 | INFO 23 (22) | INFO 23 (변경 안 함) |

## 변경 내용

각 노드에 관리 파일 하나를 새로 만들었다. 기존 `/etc/ssh/sshd_config`와 다른 drop-in은 수정하지 않았다.

`/etc/ssh/sshd_config.d/00-guardian-hardening.conf` (0644)
```
# Managed by server-guardian (fleet SSH audit SEC-SSH-004)
MaxAuthTries 4
PermitRootLogin no
```

모든 노드의 `sshd_config`가 첫머리에서 `Include /etc/ssh/sshd_config.d/*.conf`를 읽고, sshd는 처음 읽은 값을 쓴다.
파일 이름을 `00-`으로 시작해 cloud-init 등 다른 drop-in보다 먼저 적용되게 했다.

## 절차

### 1. MaxAuthTries 4 (SEC-SSH-004)

노드마다 아래를 실행하고, 하나라도 어긋나면 파일을 지우고 중단하도록 했다.

```sh
F=/etc/ssh/sshd_config.d/00-guardian-hardening.conf
grep -Eiq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config || { echo "NO_INCLUDE"; exit 3; }
if sudo test -e "$F"; then echo "EXISTS"; sudo cat "$F"; exit 4; fi
printf '# Managed by server-guardian (fleet SSH audit SEC-SSH-004)\nMaxAuthTries 4\n' | sudo tee "$F" >/dev/null
sudo chmod 644 "$F"
if ! sudo sshd -t; then sudo rm -f "$F"; echo "SYNTAX_FAIL_REVERTED"; exit 5; fi
V=$(sudo sshd -T 2>/dev/null | awk '/^maxauthtries /{print $2}')
if [ "$V" != "4" ]; then sudo rm -f "$F"; echo "NOT_EFFECTIVE_${V}_REVERTED"; exit 6; fi
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
echo "OK maxauthtries=$V"
```

1. canary: site-b-ec2 적용 → `OK maxauthtries=4`, 재감사(새 SSH 접속) 75 → 85
2. 나머지 22대(허브 포함) 적용 → 22대 모두 `OK maxauthtries=4`
3. fleet 재감사(`refresh=1`, 13:42 UTC): 23대 85점, 실패 0

### 2. 사전 확인: root 사용 여부

`PermitRootLogin no`로 끊길 접속이 있는지 읽기 전용으로 확인했다.

- 인벤토리 접속 계정: `ubuntu` 23대, `owner` 1대(edge-mini). root 없음.
- 저장소의 scripts, deploy, configs에 root 접속 없음.
- 노드별 `/root/.ssh/authorized_keys`: 23대 모두 키 1개이며, 모두 `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,command=...` 옵션이 붙은 클라우드 기본 차단 키(로그인 시 안내만 출력).
- root 로그인 기록(`/var/log/auth.log*` 및 최근 14일 sshd journal의 `Accepted ... for root`): 23대 모두 0건.

### 3. PermitRootLogin no (SEC-SSH-001)

관리 파일에 한 줄을 추가했다. 백업 후 검증에 실패하면 백업으로 되돌리도록 했다.

```sh
F=/etc/ssh/sshd_config.d/00-guardian-hardening.conf
sudo test -e "$F" || { echo "NO_MANAGED_FILE"; exit 3; }
if sudo grep -qi '^PermitRootLogin' "$F"; then echo "ALREADY_SET"; exit 4; fi
sudo cp -p "$F" "$F.bak"
printf 'PermitRootLogin no\n' | sudo tee -a "$F" >/dev/null
if ! sudo sshd -t; then sudo mv "$F.bak" "$F"; echo "SYNTAX_FAIL_REVERTED"; exit 5; fi
V=$(sudo sshd -T 2>/dev/null | awk '/^permitrootlogin /{print $2}')
if [ "$V" != "no" ]; then sudo mv "$F.bak" "$F"; echo "NOT_EFFECTIVE_${V}_REVERTED"; exit 6; fi
sudo rm -f "$F.bak"
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
echo "OK permitrootlogin=$V"
```

1. canary: site-b-ec2 적용 → `OK permitrootlogin=no`, 재감사 85 → 95 (14:24 UTC)
2. 나머지 22대 적용 → 22대 모두 `OK permitrootlogin=no`
3. fleet 재감사(`refresh=1`): 23대 95점, SEC-SSH-001~004 모두 통과

두 단계 모두 되돌린 노드는 없다. 단계마다 허브에서 새 SSH 접속(감사)과 운영자 PC에서 허브로의 새 SSH 접속이 정상임을 확인했다.
`reload`는 기존 SSH 세션을 끊지 않는다.

## 영향

- SSH 클라이언트가 키를 4개 넘게 차례로 시도하면 맞는 키에 닿기 전에 끊길 수 있다 (예: ssh-agent에 키가 많은 노트북).
  해당 노드에 `IdentitiesOnly yes`와 `IdentityFile`을 지정하면 된다.
- root로 SSH 접속할 수 없다. root 작업은 `ubuntu` 접속 후 `sudo`를 쓴다.

## 되돌리기

노드에서:
```sh
sudo rm /etc/ssh/sshd_config.d/00-guardian-hardening.conf
sudo sshd -t && sudo systemctl reload ssh
```
한 항목만 되돌리려면 파일에서 해당 줄만 지운 뒤 같은 검증과 reload를 한다.

## 제외 및 남은 항목

- **edge-mini**: 허브에서 호스트 이름 `edge-mini`이 해석되지 않아 감사·변경 대상에서 빠졌다(`unreachable`). 운영자 결정으로 보류.
- **SEC-SSH-005 (포트 22)**: 변경하지 않았다. 노드는 Tailscale 안쪽에서 쓰이고, 포트 변경은 servers.json·OCI 보안 목록·모든 클라이언트 설정을 함께 바꿔야 해서 효과에 비해 비용이 크다.
- **새 노드와 대시보드로 등록한 노드 (수동 단계)**: 허브 에이전트는 허브에만 설치하므로(`scripts/install-agent-service.sh`는 허브가 아닌 노드를 거부) 이 스크립트가 노드의 sshd를 건드리지 않는다.
  설치 스크립트는 허브 자신에만 같은 파일(`deploy/ssh/00-guardian-hardening.conf`)을 적용한다(끄려면 `--no-ssh-hardening`). 노드는 운영자가 허브에서 SSH로 직접 적용한다(새 자동화 없음):
  ```bash
  # 허브의 ~/server-guardian에서, 노드 <node>마다 (apply-ssh-hardening.sh는 sshd -t/-T로 검사하고 실패하면 되돌린다)
  ssh <node> 'mkdir -p ~/.guardian && cat > ~/.guardian/ssh-hardening.conf' < deploy/ssh/00-guardian-hardening.conf
  ssh <node> 'bash -s' < deploy/ssh/apply-ssh-hardening.sh   # 마지막 줄: UNCHANGED | INSTALLED | UPDATED | SKIPPED | REVERTED
  ```
  이미 변경한 23대는 위 명령을 다시 실행할 때 주석만 다른 저장소 버전으로 갱신된다(`UPDATED`). site-b-ec2에서 실제로 확인했다: 첫 실행 `UPDATED`, 두 번째 `UNCHANGED`, 적용값 그대로.

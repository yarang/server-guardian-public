# 허브 백업과 복원

[English](backup-restore.md) | 한국어

허브(`site-a-arm1`)의 상태를 매일 백업하고, 허브 디스크를 잃었을 때 복원하는 절차다. 백업은
`server-guardian-db-backup.timer`(매일 03:30 UTC, 최대 15분 무작위 지연)가 `cli/llm-agent/db_backup.py --keep 14`를
실행해 만든다. 같은 허브의 `server-guardian-ops-watch.timer`(야간 ops 점검)는 그보다 앞선 02:30 UTC에 돌므로, 그 점검이
시작한 토론은 그날 밤 백업에 들어간다. 이 문서의 명령은 모두 허브(또는 사본을 둔 노드)에서 실행하며, 복원 훈련은 실제
허브가 아닌 임시 디렉터리에서 먼저 해 본다(맨 아래 훈련 절차).

## 무엇이 백업되는가

`~/.guardian/data/backups/`에 두 종류의 파일이 14개씩(`--keep`) 쌓인다.

| 파일 | 내용 |
|---|---|
| `guardian_decisions-<날짜_시각>.db` | 결정 DB. SQLite 온라인 백업 API로 만든 일관된 사본 |
| `guardian_data-<날짜_시각>.tar.gz` | `data/` = `~/.guardian/data` 전체(`servers.json` 인벤토리, 팀·페르소나·프로젝트, 토론 작업 기록, jev ADR DB, 코드 위키 페이지 `code-wiki/`, 감사 로그 `audit/`, ops 점검 상태 `ops-watch/` 등), `fleet/` = `~/.ssh/guardian_fleet.conf`, `~/.ssh/guardian_known_hosts`(있을 때만) |

백업 서비스는 `~/.guardian/backup.env`를 읽는다. 허브 설치(`scripts/install-agent-service.sh`)가 이 파일을 쓴다.
- `GUARDIAN_BACKUP_REPLICA`: 허브 인벤토리 `~/.guardian/data/servers.json`의 허브 항목 `backup_replica`. `--offline`일
  때만 로컬 스냅샷 `configs/servers.json`을 읽는다.
- `GUARDIAN_DATA_DIR`, `GUARDIAN_DB_PATH`: `~/.guardian/agent.env`에 있으면 그대로 복사한다. 백업 서비스는 샌드박스 때문에
  `agent.env`를 읽지 못하므로, 이 복사가 없으면 위치를 바꾼 허브에서 기본 위치를 백업하게 된다.

`GUARDIAN_BACKUP_REPLICA`가 있으면 새 파일마다 `<replica>:~/.guardian/hub-backups/`에도 복사하고 그곳에서도 14개만
남긴다. 복사는 fleet 키를 ssh-agent로 쓴다(`guardian-fleet-agent.service`).

백업에 들어가지 않는 것:
- **개인키와 비밀**: `/etc/guardian/fleet_key`(fleet 개인키), `~/.ssh`의 개인키, `~/.guardian/agent.env`(AGENT_TOKEN),
  `~/.guardian/llm.env`(LLM 자격 증명). 서비스 샌드박스(`InaccessiblePaths`)가 읽지 못하게 막고, 코드도 허용 목록
  (`guardian_fleet.conf`, `guardian_known_hosts`)의 두 파일만 읽는다.
- `backups/` 자체, `code-index/`(코드 인덱스 캐시, 필요할 때 다시 만들어지며 `GUARDIAN_CODE_INDEX_MAX_AGE_DAYS`/
  `GUARDIAN_CODE_INDEX_MAX_MB`로 정리됨), 모든 `*.log`(`debate-jobs/*/output.log`가 가장 큼), 쓰는 중인 `*.partial`
  파일, 결정 DB의 라이브 파일(`guardian_decisions.db`, `-wal`, `-shm`, `-journal`). DB는 위의 일관된 `.db` 사본이
  대신한다.
- 설치된 릴리스(`~/.guardian/releases`)와 코드 저장소(`~/server-guardian`, 작업 클론): 다시 설치하고 다시 받는다(재배포, git).

`code-wiki/`(코드 위키 페이지, `~/.guardian/data/code-wiki/<프로젝트>/pages/*.md`)는 `code-index/`와 달리 **백업에
들어간다**. 사람이 고친 페이지와 LLM 세션이 시간을 들여 쓴 서술이라 다시 만들 수 없거나 비용이 든다. 같은 디렉터리의
`build.lock`은 쓰기 작업 중임을 나타내는 빈 파일이며 복원해도 무해하다(잠금은 프로세스가 쥐는 것이지 파일 내용이 아니다).
ops 액션의 노드 잠금 파일(`ops-locks/<host>.lock`)도 같은 이유로 복원해도 무해하다.

주의: 타르볼은 실행 중인 상태의 파일을 그대로 묶는다. 토론이 한창 쓰고 있는 JSON이나 jev DB는 드물게 중간 상태일 수
있으니, 복원 뒤 확인(아래 5단계)을 거친다. 타르볼에는 인벤토리와 jev 데이터가 들어 있으므로 권한은 `600`이고, 사본을 둔
노드도 `~/.guardian/hub-backups`를 `700`으로 유지한다.

## 복원 절차

### 0. 준비

복원에 쓸 백업을 고른다. 허브 디스크가 살아 있으면 `~/.guardian/data/backups/`, 잃었다면 사본 노드에서 가져온다.

```bash
# 사본 노드에서 최신 두 파일 가져오기 (허브를 새로 만들었다면 새 허브에서 실행)
REPLICA=<backup_replica 값>
ssh "$REPLICA" 'ls -1 ~/.guardian/hub-backups | tail -n 4'
mkdir -p ~/restore && cd ~/restore
scp "$REPLICA:.guardian/hub-backups/guardian_decisions-<날짜_시각>.db" .
scp "$REPLICA:.guardian/hub-backups/guardian_data-<날짜_시각>.tar.gz" .
```

허브 자체를 새로 만든 경우 먼저 Mac에서 허브를 설치한다(허브에는 아직 인벤토리 사본이 없으므로 첫 설치만 `--offline`).
`--offline`은 Mac의 로컬 스냅샷 `configs/servers.json`(git에서 무시됨, 예전에 `scripts/pull-inventory.sh`로 받아 둔 것)을
읽으므로 그 파일이 있어야 한다. 복원은 이 설치 뒤에 하므로, 설치가 만든 초기 `servers.json`은 3단계에서 백업본으로
바뀐다. 허브를 새 id로 옮길 때도 옛 허브의 인벤토리로는 역할 확인이 안 되므로 `--offline`이 필요하다.

```bash
scripts/install-agent-service.sh site-a-arm1 --offline
```

새 허브에서 이 설치는 이렇게 동작한다. 순서는 **설치(--offline) -> 복원 -> `setup-hub-ssh.sh` -> 설치 다시 실행**이다.
- 허브에 fleet 키가 없으므로 설치가 `/etc/guardian/fleet_key`에 새 키를 만든다(`deploy/hub/protect-fleet-key.sh`).
- 그 키는 아직 어느 노드에도 등록되지 않았으므로 설치 끝의 격리 확인에서 fleet SSH가 실패하고 설치가 종료 코드 1로
  끝난다. 4단계 전까지는 **예상된 결과**다.
- 4단계에서 `setup-hub-ssh.sh`로 키를 노드에 등록한 뒤 설치를 한 번 더 실행하면 격리 확인이 통과한다.

### 1. 허브 에이전트와 야간 점검 중지

복원 중에 허브 에이전트와 토론 작업이 파일을 쓰지 않게 한다. ops 점검 서비스는 허브 에이전트를 필요로 하므로(`Wants=`) 타이머가 돌면
허브 에이전트를 다시 띄우고 토론을 시작할 수 있다. 타이머도 함께 멈춘다.

```bash
sudo systemctl stop server-guardian-ops-watch.timer
sudo systemctl stop server-guardian-agent
# 진행 중인 토론 프로세스가 남아 있지 않은지 확인
pgrep -af guardian.py || echo "없음"
```

### 2. 결정 DB 복원

현재 파일은 지우지 않고 옆으로 옮겨 둔다. `-wal`/`-shm`은 이전 DB의 것이므로 함께 치운다.

```bash
cd ~/.guardian/data
stamp=$(date +%Y%m%d_%H%M%S)
mkdir -p pre-restore-$stamp
mv guardian_decisions.db guardian_decisions.db-wal guardian_decisions.db-shm pre-restore-$stamp/ 2>/dev/null || true
cp ~/restore/guardian_decisions-<날짜_시각>.db guardian_decisions.db
chmod 600 guardian_decisions.db
python3 -c "import sqlite3; print(sqlite3.connect('guardian_decisions.db').execute('PRAGMA integrity_check').fetchone()[0])"   # ok 가 나와야 함 (허브에는 sqlite3 명령이 없음)
```

`GUARDIAN_DB_PATH`로 DB 위치를 바꿔 둔 허브는 그 경로에 복사한다(`~/.guardian/agent.env` 확인).

### 3. 데이터 디렉터리 복원

타르볼은 `data/`와 `fleet/`로 나뉜다. 먼저 임시 위치에 풀어서 내용을 보고, 필요한 것만 제자리에 복사한다.

```bash
cd ~/restore && mkdir -p x && tar -xzf guardian_data-<날짜_시각>.tar.gz -C x
ls x/data x/fleet
# DB는 2단계에서 복원했으므로 타르볼에는 없습니다. 나머지(인벤토리, 팀, 페르소나, 프로젝트, 토론 기록, jev)를 덮어씁니다.
# audit/ 는 제외합니다: 허브에 살아 있는 감사 로그(audit/exec.jsonl)가 백업보다 새로우면 덮어쓰면 안 됩니다.
(cd x/data && tar -cf - --exclude=./audit .) | tar -xf - -C ~/.guardian/data
chmod 700 ~/.guardian/data
# 백업 시점의 감사 로그는 옆 경로에 보관하고, 라이브 로그가 비었거나 잃었을 때만 직접 합칩니다.
[ ! -d x/data/audit ] || cp -a x/data/audit ~/restore/audit-from-backup
```

감사 로그를 라이브 파일 위에 복원하지 않는다. 복원은 백업 이후의 감사 기록을 지운다.

덮어쓴 파일만 바뀌고 백업 이후에 새로 생긴 파일은 남는다. 완전히 백업 시점으로 되돌리려면 `~/.guardian/data`에서
`backups/`와 `pre-restore-*`, 복원한 DB를 제외한 나머지를 비우고 복사한다.

`ops-watch/`도 백업 시점으로 돌아가므로, 그 뒤에 이미 토론을 시작한 발견 사항이 다음 야간 점검에서 다시 토론을 시작할 수
있다.

### 4. fleet SSH 파일 복원

```bash
install -m 600 ~/restore/x/fleet/guardian_fleet.conf ~/.ssh/guardian_fleet.conf
install -m 600 ~/restore/x/fleet/guardian_known_hosts ~/.ssh/guardian_known_hosts   # 타르볼에 있을 때만
touch ~/.ssh/config && chmod 600 ~/.ssh/config   # 새 허브에는 파일이 없을 수 있습니다
grep -q 'Include ~/.ssh/guardian_fleet.conf' ~/.ssh/config || sed -i '1i Include ~/.ssh/guardian_fleet.conf' ~/.ssh/config
```

`~/.ssh/known_hosts`는 백업에 없다. 대시보드로 등록한 노드는 `guardian_known_hosts`에 호스트 키가 고정되어 복원되지만,
스크립트로 옮겨 온(migrated, `accept-new`) 노드는 허브를 다시 만든 뒤 첫 접속 때 호스트 키를 새로 신뢰한다(TOFU).

fleet **개인키는 백업에 없다.** 키가 살아 있으면(`/etc/guardian/fleet_key`, `guardian-fleet-agent.service`) 그대로 쓰고,
허브를 새로 만들어 키가 없다면 Mac에서 `setup-hub-ssh.sh`로 노드에 등록한다. 이 스크립트는 노드마다 `guardian-hub-fleet`
주석이 붙은 옛 줄을 지우고 **현재 허브 키**를 넣으므로, 재설치 뒤에 `--revoke`를 따로 실행할 필요가 없다.
(실행하더라도 `--revoke`는 키 본문이 일치하는 줄만 지운다. 기본 대상은 허브의 현재 키이므로 새 키를 지운다. 옛 키를
회수하려면 `--revoke --key <옛 공개키 줄|파일|SHA256:..>`처럼 옛 키를 명시한다.)

```bash
scripts/setup-hub-ssh.sh          # 새 허브 키 발급·등록·검증 (허브 인벤토리를 읽음)
scripts/install-agent-service.sh site-a-arm1 --offline   # 격리 확인을 통과시키기 위해 다시 실행
```

키를 일부러 바꿀 때는 `scripts/setup-hub-ssh.sh --rotate`(먼저 `--dry-run`)를 쓴다. 새 키를 모든 노드에 추가하고 새 키로
로그인을 확인한 뒤에만 허브 키를 바꾸고, 로그인이 확인된 노드에서만 옛 키를 본문으로 지운다. 하나라도 실패하면 새 키를
되돌리고 옛 키를 그대로 둔다.

### 5. 허브 에이전트 시작과 확인

```bash
sudo systemctl start server-guardian-agent
systemctl is-active server-guardian-agent
sudo systemctl start server-guardian-ops-watch.timer
set -a; . ~/.guardian/agent.env; set +a
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:9870/api/status | head -c 300; echo
```

확인 목록:
- 대시보드에 복원한 노드들이 보이고(플릿), 허브 인벤토리와 같은 목록이다: `jq length ~/.guardian/data/servers.json`.
- 이전 토론 스레드·액션이 보인다: `cd ~/.guardian/current && python3 cli/llm-agent/guardian.py --actions` 와 `--sessions`.
- 노드 그룹·팀·프로젝트가 있다: `--groups`, `--teams`, `--projects`.
- 코드 위키 페이지가 있다: `cd ~/.guardian/current && python3 cli/llm-agent/guardian.py --code-wiki-list --project server-guardian`
  (JSON; `state`는 작업 클론의 HEAD 기준 `fresh`/`stale`/`broken`). 위키 명령 전체는
  [debate-code-index-wiki.ko.md](../design/debate-code-index-wiki.ko.md)의 "구현 메모 (P3)".
- fleet SSH가 된다: `ssh -o BatchMode=yes <노드 id> hostname`, 대시보드의 SSH 감사.
- 타이머 두 개가 다시 잡혀 있다: `systemctl list-timers 'server-guardian-*' --no-pager`.
- 백업이 다시 도는지 확인한다: `sudo systemctl start server-guardian-db-backup.service && ls -l ~/.guardian/data/backups | tail -n 4`.
- 야간 점검은 dry run으로 확인한다(토론을 시작하지 않음): `cd ~/.guardian/current/cli/llm-agent && set -a && . ~/.guardian/agent.env && set +a && python3 guardian.py --ops-watch --dry-run`.

문제가 있으면 `~/.guardian/data/pre-restore-<stamp>/`에 둔 이전 DB로 되돌릴 수 있다(허브 에이전트를 멈추고 2단계의 역순).

Mac의 `configs/servers.json`은 git에서 무시되는 로컬 스냅샷이므로, 복원 뒤 `scripts/pull-inventory.sh`로 허브 인벤토리를
다시 받아 둔다(다음 `--offline` 설치에 쓰인다).

## 복원 훈련 (실제 허브를 건드리지 않음)

분기마다 한 번, 최신 사본으로 복원이 되는지 임시 디렉터리에서 확인한다.

```bash
d=$(mktemp -d) && cd "$d"
scp "$REPLICA:.guardian/hub-backups/guardian_decisions-<최신>.db" .
scp "$REPLICA:.guardian/hub-backups/guardian_data-<최신>.tar.gz" .
for f in guardian_decisions-*.db; do python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(sys.argv[1], c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT count(*) FROM agent_sessions').fetchone()[0])" "$f"; done
mkdir x && tar -xzf guardian_data-*.tar.gz -C x && ls x/data x/fleet && jq length x/data/servers.json
tar -tzf guardian_data-*.tar.gz | grep -Ei 'fleet_key|id_(rsa|ed25519)|\.log$' && echo "금지된 파일이 들어 있음" || echo "비밀·로그 없음"
```

세 가지가 모두 통과해야 한다: 무결성 `ok`, 인벤토리와 팀 파일이 풀림, 비밀·로그가 없음.
훈련 날짜와 결과는 이 문서 아래에 한 줄로 남긴다.

## 실패 알림

`db_backup.py`의 종료 코드는 `1` DB 백업 실패, `2` 사본 전송 실패, `3` 데이터 타르볼 실패다(먼저 생긴 실패가 우선이며,
나머지 단계는 계속 시도한다). systemd가 실패한 service를 기록하므로 `systemctl --failed`,
`journalctl -u server-guardian-db-backup.service`로 확인한다. 아직 자동 알림은 없다.

# 플릿 관리와 운영 프로젝트

[English](fleet-and-ops.md) | 한국어

[사용 가이드](README.ko.md)로 돌아가기.

이 문서는 노드 인벤토리, 노드 그룹, 원격 진단과 SSH 감사, 운영(`kind: "ops"`) 프로젝트, 정리 액션 카탈로그, 야간
감시, 패키지 프로비저닝 및 일괄 실행 사용법을 정리한다.

## 1. 노드 인벤토리와 등록

플릿 인벤토리의 정본은 허브의 `~/.guardian/data/servers.json`(`GUARDIAN_SERVERS_FILE`)이다. 로컬 저장소의
`configs/servers.json`은 git에 추적되지 않는 `--offline`용 스냅숏일 뿐이다.

- **노드 추가·편집**: 대시보드의 **노드 > 새 노드 등록**(`#/fleet/nodes/new`, 허브 SSH 설정을 읽고 프로브 수행)을
  사용하거나 허브의 `~/.guardian/data/servers.json`을 수정한다.
- **메시 가입(tailnet)**: 가입 대기 중인 tailnet 노드는 **노드**(`#/fleet/nodes`) 화면에서 승인하거나 해제할 수
  있다. 자세한 내용은 [headscale 운영](../operations/headscale.ko.md)을 참고한다.
- **로컬 스냅숏 동기화**: 허브 인벤토리를 워크스테이션으로 가져오려면 다음을 실행한다.
  ```bash
  scripts/pull-inventory.sh
  ```

## 2. 노드 그룹

노드는 허브 결정 DB에 저장되는 여러 사용자 정의 그룹에 동시에 속할 수 있다. 구성원은 다음 규칙으로 계산된다.

`구성원 = 직접 고른 노드 + 규칙에 맞는 노드 − 제외한 노드`

한 규칙 안의 조건은 AND, 여러 규칙 사이는 OR로 평가된다. 지원하는 규칙 필드는 `tier`, `arch`, `guardian_role`,
`role`(`eq`/`neq`), `tailscale_tags`, `cli_support`(`contains`/`not_contains`), `name`(`site-b-*` 같은 glob)이다.

```bash
# 그룹 목록 조회
python3 cli/llm-agent/guardian.py --groups

# 동적 그룹 생성·수정
echo '{"name":"Tier-1 ARM","rules":[{"conditions":[{"field":"tier","op":"eq","value":"Tier-1"},{"field":"arch","op":"eq","value":"aarch64"}]}]}' \
  | python3 cli/llm-agent/guardian.py --group-save --json

# 그룹 삭제
python3 cli/llm-agent/guardian.py --group-delete tier-1-arm --json
```

`--target` 옵션은 `local`/`all`, 노드 ID·이름·호스트 이름, 그룹 ID·이름(대소문자 구분 없음) 순으로 대상을 해석한다.

## 3. 플릿 진단, 원격 명령 실행, SSH 감사

```bash
# 플릿 전체, 단일 노드 또는 노드 그룹 병렬 진단
python3 cli/llm-agent/guardian.py --fleet
python3 cli/llm-agent/guardian.py --fleet --target site-b-cluster

# 여러 노드에 명령 병렬 실행
python3 cli/llm-agent/guardian.py --exec "uname -a" --target all
python3 cli/llm-agent/guardian.py --exec "df -h /" --target tier-1-arm

# 노드별 SSH 보안 감사 (SEC-SSH-001~005, 60초 캐시)
python3 cli/llm-agent/guardian.py --ssh-audit
python3 cli/llm-agent/guardian.py --ssh-audit --target site-b-cluster --refresh --json
```

- `/api/exec`, `/api/exec/remote`, `/api/fleet/exec`를 통해 실행된 모든 명령은 `~/.guardian/data/audit/exec.jsonl`에
  기록된다(`GET /api/audit`로 조회).
- SSH 감사 상태: `ok`(root/sudo로 유효 `sshd -T` 확인), `partial`(비root로 `sshd_config` 파일 직접 읽기),
  `unreachable`(SSH 실패·시간 초과), `error`. `authorized_keys`는 지문·주석·옵션만 반환하며 공개키 본문은 노출하지
  않는다.

## 4. 운영 프로젝트와 정리 액션 카탈로그

운영 프로젝트(`"kind": "ops"`)는 Git 저장소 대신 노드와 노드 그룹을 대상으로 한다. 기본 내장 프로젝트
`fleet`(`configs/projects/fleet.json`)은 허브를 제외한 모든 플릿 노드, `fleet-ops` 팀, 전체 정리 카탈로그, 자동
실행 비활성화, 야간 감시 활성화 상태로 제공된다.

운영 토론에서 토론 에이전트는 고정된 읽기 전용 ```` ```ops ```` 점검(`disk_usage`, `largest_dirs`, `largest_files`,
`docker_usage`, `journal_usage`, `package_cache`, `service_status`, `top_processes`, `uptime_load`,
`headscale_status` 등)으로 노드를 확인한다. 모든 점검은 `source=ops_check`로 감사 로그에 남고 인용 검증을 거친다.

판정자는 임의 셸 명령 대신 타입이 정해진 정리 카탈로그 액션을 제안한다.

| 액션 | 위험도 | 수행 내용 |
|---|---|---|
| `ops:docker_prune` | 낮음 / 중간 | 지정 범위의 미사용 Docker 이미지·컨테이너·캐시 정리 |
| `ops:journal_vacuum` | 낮음 | 크기 또는 기간 기준으로 `systemd-journald` 로그 정리 |
| `ops:apt_clean` | 낮음 | `apt` 패키지 캐시 비우기 |
| `ops:remove_paths` | 중간 | 보호 대상이 아닌 특정 파일·디렉터리 삭제 |
| `ops:truncate_logs` | 중간 | 비대해진 로그 파일 제자리 비우기 |
| `ops:remove_old_kernels` | 중간 | 미사용 구버전 Linux 커널 제거 |
| `ops:restart_service` | 중간 | 지정 systemd 서비스 재시작 |
| `jev_mcp:server_remediation` | 높음 | 카탈로그 밖 작업을 위한 자유 명령(항상 사람 승인 필요) |

모든 카탈로그 액션의 안전 수명 주기:
1. **금지 목록**: 시스템 디렉터리, 홈 디렉터리, `.ssh`, 비밀값으로 보이는 경로는 절대 삭제·수정할 수 없다
   (`policy.deny_paths`는 제한을 추가하기만 한다).
2. **미리 보기(preview)**: 제안 시점에 삭제·재시작 대상을 사전 측정한다.
3. **드리프트 검사(drift)**: 실행 직전에 다시 측정해 용량이 50% 넘게(동시에 256 MiB 초과) 늘었거나 새 항목이 생겼으면
   `STALE`로 전환하고 실행하지 않는다.
4. **사후 검증(verify)**: 실행 뒤 `before`, `after`, `freed`, `ok`를 기록한다. 한 노드에는 한 번에 액션 하나만
   실행된다.
5. **`policy.auto_run`**: 기본 비활성화. 프로젝트 정책에 저위험 액션(낮은 범위의 `docker_prune`, `journal_vacuum`,
   `apt_clean`)과 일일 상한(`max_auto_runs_per_day`, 1~100)을 명시하면, `jev` 게이트가 `PASSED`일 때만 사람 승인
   없이 자동 실행된다(`decided_by: auto:<project>`).

## 5. 야간 운영 감시

`server-guardian-ops-watch.timer`가 매일 `02:30 UTC`에 자동 실행된다.

```bash
# 토론을 시작하지 않고 발견 항목만 보고
python3 cli/llm-agent/guardian.py --ops-watch --dry-run

# 특정 운영 프로젝트만 수동 감시 실행
python3 cli/llm-agent/guardian.py --ops-watch fleet
```

`policy.watch`가 켜진 각 운영 프로젝트의 대상 노드에서 디스크·inode 사용률(기본 `85%`)과 저널 크기(기본 `3G`)를
점검하고, 새로 발견된 이상 항목에 대해 운영 토론을 1회 시작한다(동일 항목 7일 중복 방지).

## 6. 플릿 프로비저닝과 일괄 실행

### 프로비저닝 (`fleet-provision` 팀)
`ops:package_install` / `ops:package_remove`를 허용한 운영 프로젝트는 `fleet-provision` 팀으로 여러 노드의 패키지
설치·삭제를 계획할 수 있다.
- 내장 패키지 보호(`configs/package-roles.json` + 노드의 `ops_roles` + `policy.protected_packages`)가 핵심 패키지
  삭제를 차단하고, 열려 있는 포트의 소유 패키지를 식별한다.
- 설치 번들(`configs/package-bundles.json`, 예: `nginx`, `postgresql`)은 검증된 패키지 목록을 제공한다.
- 다중 노드 제안은 순서가 있는 **계획(plan)**으로 저장되며 **승인** 화면이나 CLI에서 관리한다.
  ```bash
  python3 cli/llm-agent/guardian.py --plans [<스레드ID>]
  python3 cli/llm-agent/guardian.py --plan-show <계획ID>
  python3 cli/llm-agent/guardian.py --plan-run <계획ID>
  python3 cli/llm-agent/guardian.py --plan-reject <계획ID>
  ```

### 명령 세트와 일괄 실행
재사용 명령 세트는 `configs/command-sets/`(`health-report`, `service-status`, `refresh-package-index`)에 내장되어
있으며 **노드 > 명령 세트**에서 새로 저장할 수 있다.

```bash
python3 cli/llm-agent/guardian.py --command-sets
python3 cli/llm-agent/guardian.py --command-set-confirm <ID>
```

`ops:run_command_set` / `ops:run_script`를 허용한 프로젝트는 비평가 **투표**와 **배치 게이트**로 다중 노드 계획을
심사한다.
- 운영자가 저장한 명령 세트는 무인 일괄 실행 전 본문 확정(`--command-set-confirm` 또는 대시보드 확정)이 필요하다.
- 자유 스크립트(`ops:run_script`)는 기본적으로 항상 사람 승인을 기다린다(`policy.batch.scripts: "approve"`). 허브가
  아닌 그룹에 `"vote"` 모드와 `batch.auto`를 명시적으로 켠 경우에도 비평가 반대가 하나라도 있으면 사람 결정으로
  넘어가며, `eval`이나 셸 파이프 같은 우회 패턴은 제안 단계에서 거부된다.

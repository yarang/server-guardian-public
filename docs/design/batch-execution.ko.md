# 일괄 실행: 여러 노드에 명령 세트를 동시에, 비평가가 표결하고 반대할 때만 사람이 결정

[English](batch-execution.md) | 한국어

상태: 제안됨 (2026-10-20, 2장의 범위 결정 세 가지는 같은 날 운영자가 내렸고, 11장의 "확인 필요" 항목은 제 제안이다). 관련
문서: [fleet-provisioning.ko.md](fleet-provisioning.ko.md) (계획, 이 문서가 기반으로 삼는 패키지 도구),
[server-ops-project.ko.md](server-ops-project.ko.md) (ops 프로젝트, `auto_run`),
[multi-perspective.ko.md](multi-perspective.ko.md).

## 1. 문제

지금 ops 프로젝트는 타입이 정해진 액션를 하나씩 다루고, 모든 계획은 사람을 기다린다("계획은 사람의 승인 없이 실행되지
않는다"). 운영자는 노드 관리에서 기본값을 반대로 바꾸기를 원한다. 시스템이 *일괄 작업*(저장된 명령 세트나 스크립트)을 여러
노드에 동시에 실행하고, 비평가들이 위험을 판단하며 필요하면 노드의 실제 상태를 먼저 확인한 뒤 *표결*하고, 누군가 반대할
때만 사람에게 묻는다.

위험은 분명하다. 비평가는 언어 모델이고, 그들의 "찬성"이 사람의 클릭을 대신하게 되며, 그들이 읽는 글은 노드(와 명령문)에서
온다. 그래서 이 설계는 모든 강제 규칙을 모델 밖의 코드에 두고, 표결은 사람에게 묻는 이유를 *더하는* 데만 쓴다. 코드가
요구하는 이유를 *없애는* 데는 쓰지 않는다.

## 2. 결정

2026-10-20 운영자가 세 질문에 답해 내린 결정:

1. **명령의 범위: 저장된 명령 세트와 즉석 스크립트.** 운영자가 명령 세트를 등록하고, judge가 그 자리에서 스크립트를 쓸 수도
   있다.
2. **되돌릴 수 없는 작업은 항상 사람의 승인이 필요하다.** 되돌릴 길이 없거나 위험이 high인 단계(데이터 삭제, 패키지 삭제,
   재부팅, 보호 대상)는 비평가가 모두 찬성해도 승인이 필요하다. 나머지는 반대하는 비평가가 없으면 바로 실행한다.
3. **한 명의 반대로 충분하다.** 노드 상태 점검을 인용한 비평가 한 명의 반대가 있으면 계획은 사람에게 간다. 아무것도
   인용하지 않은 반대는 조언으로만 남긴다.

4. **자유 스크립트: 노드 그룹별 옵트인 (2026-10-20, 결정 2를 바꾼다).** `fleet-provision` 토론에서 judge가 저장된 세트가
   없다는 이유로 평범한 작업(시간대, 설정 파일)을 거부하는 것이 드러났고, 운영자는 프로젝트가 지정한 노드 그룹에서만
   비평가의 표결로 즉석 스크립트를 풀어 주는 데 동의했다. 그 밖에서는 되돌릴 수 없는 작업은 여전히 사람이 승인한다. 저장된
   세트가 여전히 권장 형태다. 4.3과 12.5를 본다.

제가 이끌어 낸 결론(확인 필요, 11장): 즉석 스크립트는 읽는 것만으로 되돌릴 수 있음을 증명할 수 없으므로 결정 4가 적용되지
않는 한 사람에게 간다(4.3). 유효한 표를 내지 않은 비평가는 찬성으로 센다(5.3, 2026-10-20 운영자 결정). 스위치는 프로젝트별 정책이며 운영자가 켜기 전까지
어디서나 꺼져 있다(7).

## 3. 설계 그림

```
명령 -> expert -> 비평가 -> judge: 최종 계획 (단계 = 타입 액션, 명령 세트, 스크립트)
                                      |
                       계획 해시 (정규화한 계획의 sha256)
                                      |
                  표결 단계: 모든 비평가가 "최종 계획"을 읽고, ops 점검을 실행할 수 있고, 표를 낸다
                                      |
                       관문(순수 코드): 정책 + 표 + 가역성 표 + 상한 + jev
                          |                                   |
                   모든 조건 충족                          하나라도 실패
                          |                                   |
        계획이 autoplan:<project>로 즉시 실행           계획은 PROPOSED로 남고, 관문의 사유가
        (노드 병렬, 단계마다 재시뮬레이션)          옆에 표시된다. 사람이 결정한다
```

명령을 실행하는 새 경로는 없다. 일괄 작업은 평범한 계획이다. 단계는 `decision_actions` 행이고, 노드는 병렬로, 노드당
한 번에 한 단계씩 돌며, 각 단계는 실행 직전에 다시 시뮬레이션되고 달라졌으면 `STALE`로 끝난다(`plan_ops.py`). 새로운 것은
(a) 두 가지 단계 종류, (b) 최종 계획에 대한 표결, (c) 사람 없이 계획을 시작할 수 있는 관문이다.

## 4. 단계가 될 수 있는 것

### 4.1 기존 타입 액션
`ops:*` 카탈로그 액션는 그대로다. 각각의 가역성은 6장의 표에 있다.

### 4.2 `ops:run_command_set`
`{target_host, set, params}`. 명령 세트는 운영자가 쓰고 검토한 파일이다. 기본 제공 세트는 `configs/command-sets/*.json`(git,
적고 범용적), 운영자 자신의 세트는 `<data dir>/command-sets/*.json`에 둔다.

```json
{"id": "refresh-index", "description": "패키지 인덱스 갱신", "os": ["linux"], "needs_sudo": true,
 "params": {"older_than_days": {"type": "int", "min": 1, "max": 365}},
 "check": "ls -l /var/lib/apt/lists | head -5",
 "script": "set -eu\napt-get update -q",
 "reversible": true, "undo": "되돌릴 것 없음, 다음 갱신 때 인덱스가 다시 만들어진다",
 "risk": "low", "timeout": 600, "reviewed": "2026-10-20"}
```

- 매개변수는 타입이 있다(`int`, `enum`, `unit`, `package`, `path`, 정규식을 갖는 `string`). 스크립트는 `${NAME}`을 쓰고
  엔진이 값을 엄격한 문자 집합(영문자, 숫자, `._:/@+=,-`, `..` 구간 없음, `-`로 시작 금지)에 맞는 맨 토큰으로 넣는다.
  `string` 값은 64자로 제한되고, `(a+)+`처럼 반복이 중첩된 패턴은 등록 때 거부된다. 선언 없는 자리표시자, 모르는 매개변수, 타입에 맞지 않는 값은 단계를 거부한다. `path`
  값은 `ops_paths`(기본 거부 규칙, `policy.deny_paths`)를 통과해야 한다.
- 등록할 때 `bash -n`을 돌리고, 스크립트를 8 KiB로 제한하고, `reversible: true`에 비어 있지 않은 `undo`가 있는지 보며,
  `risk`가 `high`면 `reversible: false`로 강제한다. 선언은 운영자의 진술이고, 검증기는 선언이 일관된지만 본다.
- `check`는 읽기 전용 스크립트(운영자가 그렇게 선언한다)이며, 그 출력이 단계의 *미리보기*가 되어 단계와 함께 저장되고
  실행 직전에 다시 비교된다. `check`는 단계를 **제안할 때**, 아직 아무도 보기 전에 실행되므로, 세트가 `check_sudo: true`
  (`needs_sudo` 필요)를 함께 선언하지 않으면 root 없이 돈다. 미리보기에는 렌더링된 스크립트의 sha256도 들어가므로, 제안 뒤에 세트를 고치면 그 단계는
  `STALE`이 된다.
- 감사 줄에는 `source=action`, `op=run_command_set:<id>`(레이블은 64자 이하), `action_id`가 붙는다. 허브는 명령을 4096자에서
  자르고 스크립트는 base64로 가므로 감사 줄만으로는 긴 스크립트를 복원할 수 없다. 그래서 단계의 미리보기와 결과에
  렌더링된 `script`, `check`와 `script_sha`를 함께 보관한다.

### 4.3 `ops:run_script` (즉석)
`{target_host, script, reason}`, 최대 4 KiB, 노드에서 `bash -c`로(고정 래퍼 안의 base64로) 실행한다. judge가 쓴 것이므로 아무것도 보증되지 않는다.
**항상 high이고 되돌릴 수 없는 것으로 평가하므로 사람에게 간다.** 단 `policy.batch.scripts`가 `"vote"`인 프로젝트의
`policy.batch.script_groups` 노드에서는 예외다(결정 4, 12.5). 거부 목록(`rm -rf /`, `mkfs`, `dd of=/dev/…`, 보호
unit 정지·비활성화, Guardian이나 자격 증명 경로 접근, `curl | sh`)에 걸리면 사람이 보기도 전에 단계가 `INVALID`가 된다.
이 목록은 `/bin/rm`, `\rm`, `sudo -u x`, `timeout`, `nice`, 단어 안의 따옴표까지 읽지만, 뻔한 실수를 막는 최선 노력의
경보선이지(`cat ~/.gu*/agent.env`나 `sh""adow` 같은 글롭·따옴표 우회는 통과한다) 안전 경계가 아니며, 문서도 그렇게 말한다. 경계는 사람의 승인이다. 승인한 뒤에는
대시보드가 "명령 세트로 저장"을 제안하고(B4단계), 좋은 스크립트는 그렇게 자동 경로로 승격된다.

## 5. 표결

### 5.1 표는 최종 계획에 대해서 낸다
비평가는 expert의 *제안*을 보고, 계획은 그 뒤에 judge가 쓴다. 비평 라운드에서 받은 표는 초안을 승인할 뿐 실제로 실행되는
단계를 승인하지 않는다. 그래서 표결은 judge의 계획이 파싱되고 정규화된 **다음**에 별도 단계로 한다.

- `plan_ops.plan_hash(plan)`은 정규화된 계획(목표, `stop_plan`, 단계별 id·host·tool·args·requires·on_failure, 명령 세트는
  렌더링된 스크립트의 sha256)의 정규 JSON에 대한 sha256이다.
- 각 비평가에게 자기 세션에서 한 번씩 묻는다(이미 아는 것을 유지하고 ```ops 점검을 실행할 수 있다). 단계들과 해시의 앞
  12자를 읽고 다음 블록으로 답하게 한다.

```vote
{"plan": "3f9a1c0be712", "vote": "approve", "reason": "…"}
{"plan": "3f9a1c0be712", "vote": "oppose", "reason": "n2의 디스크가 97% 찼다", "cites": ["[#4 disk_usage n2]"]}
```

- 표는 자신이 적은 해시에만 유효하다. 이후 라운드에서 계획이 바뀌면 이전 표는 무효가 되고 표결을 다시 한다.

### 5.2 무엇이 반대로 인정되는가
`oppose`의 `cites` 중 하나 이상이 그 비평가가 이 토론에서 받은 점검 결과일 때(`OpsChecks.unverified_citations`, 다른 모든
인용과 같은 검증) 반대로 인정된다. 검증된 인용이 없는 `oppose`는 조언으로 저장되고, 계획이 어차피 사람에게 가면 함께
보여 주지만 실행을 막지는 않는다. 비평가 프롬프트는 반대의 값어치를 말해 준다. 이름 붙은 노드에서 인용으로 뒷받침되는
구체적 피해이지 취향이 아니다(프로비저닝 팀의 "[차단]").

### 5.3 없는 표
파싱 가능한 표를 한 번의 수정 요청 뒤에도 내지 않았거나 다른 해시를 적은 비평가는 **기권**이고, 기권은 *찬성*으로
센다(2026-10-20 운영자 결정: 실패하거나 시간 초과한 비평가가 일괄 작업을 붙잡아서는 안 된다). 그 결과 고장 난 비평가는
더 이상 계획을 막을 수 없으므로, 스스로 실행되는 계획의 안전은 코드 관문(가역성 표, 거부 목록, 상한, 재시뮬레이션)에만
달려 있다. 기권은 `vote: "abstain"`으로 저장하고 보여 주어서 사람과 그림자 모드 비교가 얼마나 자주 생기는지 볼 수 있게
한다. 모든 비평가가 기권하면 관문은 "표가 하나도 없었다"는 사유를 덧붙이되 계획은 그대로 실행한다.

### 5.4 비용
비평가마다 짧은 턴이 하나 늘며, `policy.batch.auto`가 있는 프로젝트(또는 B2의 그림자 모드)에서만 쓴다. 선택하지 않은 팀과
프로젝트의 프롬프트는 바이트 단위로 같고, `PromptGateTests`가 이를 고정한다.

## 6. 가역성은 추론이 아니라 표다

`risk()`로는 부족하다. 패키지나 경로 삭제는 *medium*일 뿐이다. 관문은 명시적인 표(`batch_gate.REVERSIBLE`)를 읽고, 모르는
도구는 되돌릴 수 없는 것으로 본다.

| 단계 | 가역 | 이유 |
|---|---|---|
| `ops:apt_clean`, `ops:docker_prune` (`dangling_images`, `build_cache`) | 예 | 다시 만들어지는 캐시 |
| `ops:docker_prune` (`stopped_containers`, `unused_images`) | 아니오 | 쓰기 레이어와 로컬 빌드가 사라진다 |
| `ops:journal_vacuum`, `ops:truncate_logs`, `ops:remove_paths` | 아니오 | 데이터를 지운다 |
| `ops:remove_old_kernels`, `ops:package_remove` | 아니오 | 롤백 없음 |
| `ops:package_install` | risk가 high가 아니면 예 | 다시 지울 수 있다. bun·pnpm·yarn·pipx·uv와, 시뮬레이션이 패키지를 함께 지우는 설치는 high |
| `ops:restart_service` | 예 | 상태가 일시적이고 반복 가능하다. 보호 unit은 어차피 코드가 거부한다 |
| `ops:run_command_set` | 세트의 선언, risk가 high면 항상 아니오 | 4.2 |
| `ops:run_script` | 아니오 | 4.3 |

재부팅은 카탈로그에 없다. 재부팅하는 명령 세트는 `reversible: false`로 선언한다. 보호 패키지와 보호 unit은 지금의 관문이
계속 **거부**한다(단계가 `INVALID`가 된다). "사람에게 묻는다"보다 엄격하다.

## 7. 관문

`batch_gate.decide(plan, steps, votes, policy, runs_today, jev_passed) -> {"auto": bool, "reasons": [...]}`은 순수 함수다.
**모든** 조건이 맞을 때만 계획이 사람 없이 시작되며, 실패한 조건은 사유 목록에 모두 적힌다.

1. 프로젝트가 켰다: `policy.batch.auto`가 true;
2. `GUARDIAN_AUTO_APPLY`와 `GUARDIAN_UI_ACTIONS`가 `off`가 아니다;
3. 실제 jev PASSED다(계획이 `HELD`가 아니다. jev 관문이 꺼져 있으면 스스로 실행되는 것은 없다);
4. 라인업의 어떤 비평가도 이 계획 해시에 인정되는 반대를 내지 않았다(5.2). 기권은 찬성으로 센다(5.3);
5. 되돌릴 수 없거나 high인 단계(6장)가 없고 `INVALID`인 단계도 없다;
6. 계획의 단계가 `policy.batch.max_steps` 이하이고 오늘(UTC) 자동으로 실행된 계획이 `policy.batch.max_plans_per_day`보다
   적다;
7. 실행 시점에 모든 단계가 다시 시뮬레이션되고 달라졌으면 다른 계획처럼 `STALE`로 끝난다.

정책(`project_registry`에서 검증, 모르는 키는 거부, 기본은 꺼짐):
`"batch": {"auto": false, "max_plans_per_day": 0, "max_steps": 20, "max_parallel_hosts": 2}`. 상한은 단계가 아니라 **계획**을
센다(20단계 계획 하나가 상한 3을 다 쓰게 되기 때문이다). `max_parallel_hosts`(1~8, 기본 2)는 상수 `MAX_PARALLEL_HOSTS`를
대체한다. 허브는 OCPU가 둘이므로 기본값은 낮게 둔다.

관문을 여는 변경은 `action_ops.auto_run_actions` 한 곳이다. `plan_id` 건너뛰기("계획 단계는 사람이 승인한 계획과 함께만
실행된다")는 관문이 풀어 준 계획 **하나를 빼고** 그대로 유지하며, 풀린 계획은 단계별이 아니라 항상 `plan_ops.run_plan`으로
통째로 실행한다.

## 8. 감사 기록

- 자동 계획의 행위자는 `autoplan:<project>`(모든 단계의 `decided_by`, 12.3 참고), 그 밖에는 사람의 이름이다.
- `decision_plans`에 `plan_hash`, `votes`(JSON: 비평가, 표, 사유, 검증된 인용, 인정 여부), `gate`(JSON: `auto`, `reasons`,
  `at`)가 생긴다. 기존 DB는 단계 열처럼 `ALTER TABLE … ADD COLUMN`으로 이전한다.
- 이벤트: `plan_vote`(비평가별), `plan_gate`(결정과 사유), `plan_auto_run`(시작, 끝).
- 허브 감사 줄은 `action_id`를 유지한다. 이 설계의 어떤 테스트도 허브 데이터 디렉터리에 쓰지 않는다.

## 9. 대시보드

계획 화면에 관문의 결정과 사유, 비평가별 표(사유와 인용한 점검 포함, 조언은 조언으로 표시), 계획 해시, 단계별 가역성이
나온다. 사람을 기다리는 계획은 *왜*인지 말해 준다("비평가 2 반대: n2의 디스크가 97% 찼다", "단계 s3은 되돌릴 수 없다").
새 문자열은 모두 두 카탈로그(`ko`, `en`)에 넣는다.

## 10. 단계

- **B1: 명령 세트와 두 단계 종류.** `command_sets.py`, `ops:run_command_set`, `ops:run_script`,
  `policy.batch.max_parallel_hosts`, `--command-sets`(목록, 검증). 자동 실행은 없다. 테스트 먼저, 격리된 데이터 디렉터리.
- **B2: 표결과 관문, 그림자 모드.** `plan_hash`, 표결 단계, `batch_gate.decide`, 스키마 열, 이벤트, 켠 라인업에만 프롬프트
  버전 p17. 관문은 계산되어 저장되지만 계획을 시작하지는 않는다. 운영자가 "실행됐을 계획"과 실제로 한 일을 비교한다.
  모델 없는 코퍼스(`batch_corpus.py`, `package_corpus.py`와 같은 방식)가 관문의 조합을 붙든다. 차단 조건 각각의 재현율
  1.00, 오차단율 0.00, 모든 비평가가 기권하는 경우.
- **B3: 자동 실행(2026-10-20 구현, 배포 전).** 풀린 계획의 경로, 행위자, 상한, 끄는 스위치, 이벤트. 운영자가 프로젝트에 `policy.batch.auto`를 켜기
  전까지는 꺼져 있다.
- **B4: 대시보드(2026-10-20 구현, 배포 전).** 관문 화면(9장), "명령 세트로 저장", 설정의 명령 세트 편집기.

각 단계는 커밋 전에 자체 보안 검토(원격 실행, sudo, 승인 로직)를 받고, 깨끗한 worktree에서 전체 `scripts/ci.sh`를 돌리며,
배포는 요청이 있을 때만 한다.

## 11. 열린 문제 (확인 필요)

1. **어느 프로젝트가 `policy.batch.auto`를 갖는가?** 제안: 기본 제공 `fleet`을 포함해 기본적으로 없음. 그림자 모드(B2)를
   거기서 먼저 돌린다.
2. **즉석 스크립트는 사람에게 간다**(4.3). 운영자가 옵트인한 그룹은 예외다(결정 4). 정적 파서를 안전 경계로 삼는 것은 여전히
   믿을 수 없다고 보므로, 옵트인은 경보선이 아니라 비평가의 검토, 카나리 노드, 운영자 자신의 거부 목록에 기댄다.
3. ~~없는 표는 실행을 막는다~~ 2026-10-20 결정: **없는 표는 기권이고 찬성으로 센다**(5.3).
4. **가역성 표**(6장), 특히 `restart_service`와 `package_install`을 가역으로 본 것.
5. **상한은 계획을 센다**(기본 0 = 정하기 전까지 없음).
6. 표결 단계가 비평가의 기존 세션을 쓸지(싸고 기억이 있다) 새 세션을 쓸지(앞서 주장한 것과 독립적). 제안은 기존 세션이다.

## 12. 구현 메모

### 12.1 B1 (2026-10-20 구현, 배포 전)

- `command_sets.py`(표준 라이브러리만)가 저장소, 매개변수 형식, 래퍼, 검증을 맡는다. 내장 세트는
  `configs/command-sets/*.json`(`health-report`, `service-status`, `refresh-package-index`), 운영자 세트는
  `<GUARDIAN_DATA_DIR>/command-sets/*.json`이며 두 곳에 같은 id가 있으면 오류다. `guardian.py --command-sets`가 목록과
  검증을 보여 주고, `scripts/ci.sh` config 단계가 이를 실행해 180일 넘게 검토하지 않은 세트를 경고한다.
- 설계와 달라진 점: 도구 게이트가 옵트인이다. ops 프로젝트의 기본 `policy.allowed_actions`에서 패키지 도구와
  `ops:run_command_set` / `ops:run_script`를 뺐으므로 `fleet-ops` 프롬프트는 바이트 단위로 그대로다(p16 고정).
  프로젝트(B2에서는 팀 구성)가 제공할 도구를 명시한다.
- 전송: 스크립트는 base64로 인코딩해 고정된 `bash -c "$(printf %s '<b64>' | base64 -d)"` 래퍼 안에 실려 가므로, 매개변수나
  스크립트 텍스트가 셸 명령줄에 끼어드는 일이 없다. 매개변수는 `SAFE_VALUE`에 맞는 형식 있는 토큰(`int`, `enum`,
  `unit`, `package`, `path`, 패턴이 있는 `string`)이고, `path`는 `ops_paths.refusal`도 거친다.
- `drift()`는 저장된 스크립트와 check의 `script_sha`를 비교한다. 제안 뒤에 편집된 세트는 `STALE`이다. `check`가 있는 세트는
  미리보기에서 한 번(60초 제한), 실행 뒤 한 번 더 돌고, 스크립트는 미리보기에서 실행하지 않는다.
- `risk` / `reversible`은 세트의 선언을 따르고 `risk: high`는 비가역으로 고정된다. 즉석 스크립트는 항상 high이며 비가역이다.
  `ops_auto_approvable`은 두 도구를 명시적으로 거부한다(B3에서는 하나씩이 아니라 계획의 게이트가 풀어 준다).
- `policy.batch.max_parallel_hosts`(1-8, 기본 2)는 `plan_ops.parallel_hosts`를 통해 `plan_ops.run_plan`에 닿는다
  (`run_plan(max_parallel=None)`이 계획의 프로젝트에서 값을 구한다). B2가 `auto`, `max_plans_per_day`, `max_steps`를 추가하기
  전까지 알 수 없는 `policy.batch` 키는 거부한다.
- 리뷰 반영(코드·보안 리뷰, 2026-10-20): sha는 스크립트, check, 단계의 실행 방식을 정하는 모든 필드(`needs_sudo`,
  `check_sudo`, `os`, `timeout`, `risk`, `reversible`)를 덮는다. 미리보기를 미룬 계획 단계도 sha를 유지하고 세트가 고쳐지면
  `STALE`이 된다. check, 실행, 두 번째 check는 하나의 `Plan`을 공유한다. 세트가 렌더링된 텍스트는 경보선을 다시 거친다
  (값이 `rm -rf /srv/${T}`를 완성할 수 있다). 래퍼는 스크립트를 해독하지 못하면 7로 종료한다. `load_sets`는 파일(이름,
  mtime, 크기, inode) 단위로 캐시하고 점 파일을 무시하며 심볼릭 링크와 64 KiB 초과 파일을 거부한다. 허브는 명령당 stdout,
  stderr를 각각 최대 1 MiB만 보관한다(`apps/agent/capped_buffer.go`).

### 12.2 B2 (2026-10-20 구현, 배포 전)

- `batch_gate.py`(순수 함수, 모델·노드 없음)가 판정하고, `batch_votes.py`가 묻고 해석하고 세며, 토론 엔진의
  `_vote_pass`가 둘을 잇는다. 입력은 `plan_ops.plan_hash`, `plan_ops.gate_steps`, `plan_ops.auto_plans_today`가 댄다.
  `decision_plans`에 `plan_hash`, `votes`, `gate` 열이 생겼고(옛 데이터베이스는 열 때 마이그레이션), 이벤트는
  `plan_vote`, `plan_gate`다.
- 설계와 달라진 점: `plan_hash`는 판사의 JSON이 아니라 저장된 계획 행에서 계산하며(`plan_ops.plan_hash(db, plan_id)`)
  행 id와 무관하다. 그래서 표결은 `record_plan`이 계획을 저장한 뒤에 받고, 표는 그 해시의 앞 12자에 대해서만 유효하다.
  다른 계획을 가리키는 표는 기권이다.
- 설계와 달라진 점: 프롬프트 버전 p17로 올리지 않았다. 표결 형식은 표결 차례의 텍스트에만 있고(`format_reminder`로
  전달하므로 엔진의 ops 인용 보정이 표결 블록을 유지한다), 표결 단계는 `batch_votes.votes_enabled(project)`일 때만 돈다.
  `policy.batch.auto`가 true이거나 배치 도구가 `policy.allowed_actions`에 있는 경우다. 평범한 `fleet-ops` 프로젝트는
  묻지 않으므로 프롬프트와 비용이 그대로다.
- 표결 단계는 실제 jev `PASSED` 뒤, 계획 저장 뒤에만 돌며 계획을 시작하지 않는다. 비평가는 병렬로, 각자의 세션
  (`lineup.session_key(CRITIC_ROLE, member)`; 11장 6번의 "기존 세션" 안)에서 묻고, 먼저 ```ops 점검을 돌릴 수 있다.
  쓸 수 없는 답에는 보정 요청 한 번, 그다음은 기권이다. 물을 수 없는 비평가(내장 제공자, 예외)는 메모와 함께 기권한다.
  반대는 개별로 검증된 인용이 하나 이상 있어야 센다(`OpsChecks.unverified_citations`가 하나 이상 찾고 잘못된 것이 없음).
  인용이 전혀 없는 글은 깨끗한 인용이 아니다. 지어낸 인용은 엔진 자체의 CITATION CHECK 재질문을 먼저 거친다.
- 그림자 모드는 저장된 관문의 `would_auto` 필드다. `policy.batch.auto` 옵트인만 빼고 모두 평가하며 끄는 스위치는 있는
  그대로 반영한다. 별도의 `shadow` 정책 키는 없다. `max_plans_per_day`를 정한 적 없는 프로젝트(0)는 그림자에서 상한 없이
  판정하지만, 옵트인하고 상한 0이면 자동 실행은 없다. 하루 횟수는 처음에는 `auto:<project>`가 결정한 행동으로 셌고,
  B3부터는 저장된 관문 기록에서 시작된 계획을 센다(12.3).
- 아무도 표를 내지 않으면 관문은 `no vote was cast` 메모를 붙이고 그래도 풀어 준다(2026-10-20 결정).
- `batch_corpus.py`(정상 계획 12개, 심은 결함 29개, 모델·노드 없음): 재현율 1.00, 오차단율 0.00(`--targets`,
  `test_batch_corpus.py`가 `scripts/ci.sh python`에서 붙든다).
- 리뷰 반영(코드 리뷰, 2026-10-20): `OpsChecks.run`은 호출 번호를 잠금 아래에서 매긴다(비평가들이 점검을 병렬로 돌리므로,
  전에는 결과가 다른 호출의 번호로 기록되어 정당한 반대가 검증 불가로 버려지고 기권 곧 찬성으로 셌다). 표결 단계는 jev
  게이트가 있고 통과했을 때만 돈다. 표결 단계 안의 실패가 토론을 끝내지 않고, 계획은 사람이 결정하도록 하는 관문 기록을 받는다.
  미리보기를 미룬 패키지 단계는 측정되지 않은 것으로 보고 사람에게 간다. 표는 계획 해시 전체를 적어도 된다. `plan_vote`,
  `plan_gate` 이벤트에 사유, 메모, 해시가 들어 있다.
- 리뷰 반영(보안 리뷰, 2026-10-20): 유효한 표 블록이 여럿이면 신중한 쪽으로 읽는다(유효한 반대가 하나라도 있으면 찬성보다
  우선하므로 인용되었거나 주입된 찬성 블록이 반대를 지울 수 없다. 깨진 블록은 다른 유효한 블록이 있으면 무시한다). 기권으로
  처리되는 것은 응답하지 못한 비평가뿐이고, 파싱·검증·저장 중의 버그는 실패 시 닫히는 `_vote_pass`로 올라간다. 비평가는
  저장된 스크립트와 인자를 본다(스크립트 8192자, 인자 2000자까지, 잘린 부분은 `(cut)`으로 표시). 미리보기가 오류인 패키지
  단계도 미룬 것과 같이 측정되지 않은 것으로 본다. 패키지 설치는 시스템 관리자(apt, dnf 등)이거나 `ignore_scripts`일 때만
  되돌릴 수 있는 것으로 본다(npm, brew 등은 제거로 되돌려지지 않는 제3자 코드를 실행하기 때문이다). 관리자가 아직 `auto`인
  설치는 사람에게 간다.
- 운영자 결정이 남은 것: 모든 비평가가 기권해도 계획은 풀린다(2026-10-20 결정이므로 정족수는 0이다). 반대는 노드 점검을
  인용해야 하므로 내용만 보고 하는 우려(읽기에 이상한 스크립트)는 조언으로 남는다. `restart_service`는 되돌릴 수 있는 것으로
  센다(미결 4).
- B4로 넘긴 것(12.4에서 해결): 대시보드 ops 편집기는 저장할 때 `policy`를 새로 만들어 `policy.batch`를 잃었다. 하루 횟수, 행위자, 실행 시점 위험도 확인에 대한 B2의 메모는 12.3에서 해결되었다.

### 12.3 B3 (2026-10-20 구현, 배포 전)

- `batch_auto.release(db, mcp, plan_id)`가 풀린 계획을 실행한다. 저장된 관문이 `auto`이면 토론 엔진이 표결 단계 직후
  `_auto_run_plan`에서 부른다. 계획은 토론 프로세스 안에서 동기적으로 돌므로 계획이 끝나야 토론이 반환한다(토론 작업은 기본
  1시간인 `JobTimeout` 뒤에 끊긴다. 그때 `RUNNING`으로 남은 단계는 `run_plan`이 실행 중인 단계 위에서 다시 시작하지 않으므로
  사람을 기다린다). 풀어 주다가 실패해도 토론은 끝나지 않는다. 계획은 사람을 위해 `PROPOSED`로 남고 이유는 `plan_auto_run`
  이벤트와 결과에 들어간다.
- 저장된 것은 그대로 믿지 않는다. `release`는 `auto`가 참인 저장된 관문 기록을 요구하고, `plan_ops.plan_hash`를 다시 계산해
  관문과 `plan_hash` 열 둘 다와 비교하며, 계획에서 프로젝트를 구하고(ops 프로젝트만), 저장된 단계와 표로 지금의 정책, 끄는
  스위치, 하루 횟수 아래에서 관문을 다시 판정한 뒤(`jev_passed=True`: 표결 단계는 통과한 jev 게이트 뒤에서만 돌았고
  `HELD` 단계는 준비된 것이 아니다) 그제야 시작을 선점한다.
- 시작은 쓰기 트랜잭션 하나로 선점한다(`DecisionDBManager.claim_auto_plan`, `BEGIN IMMEDIATE`). 이미 시작한 계획과 하루
  `max_plans_per_day`를 다 쓴 프로젝트를 거절하고 `gate.auto_run = {actor, started_at}`을 쓴다. 하루 횟수
  (`count_auto_plans`, `plan_ops.auto_plans_today`)는 이 기록의 `started_at`을 읽으므로 나중의 사람 조작이 바꿀 수 없다. 결과가
  어떻든 시작은 소모되고, 실행이 끝나면 `finish_auto_plan`이 `finished_at`과 `counts`(또는 `error`)를 더한다.
- 이탈: 단계는 `auto:<project>`가 아니라 `autoplan:<project>`로 실행된다. 앞의 것은 단일 행동 상한(`max_auto_runs_per_day`,
  `action_ops.auto_runs_today`)에 센다. 두 상한은 서로 독립으로 유지된다.
- 실행 시점 보호: `plan_ops.run_plan`은 `guard(action, measured_now)`를 받아, 노드 잠금 아래에서 단계를 다시 측정하고
  드리프트를 확인한 뒤 실행 직전에 묻는다. `batch_auto.step_guard`는 그 새 측정으로 단계의 위험도를 매기고
  (`plan_ops.gate_item(row, preview)`) `batch_gate.step_problem`에 묻는다. 이제 high 위험이거나 측정되지 않았거나(패키지 도구)
  되돌릴 수 없는 단계는 이유와 함께 `STALE`로 끝나고 그것에 의존하는 단계는 `SKIPPED`가 된다.
- 이벤트: `plan_auto_run`(`started`, `counts`, `reasons`, `error`). 토론 결과의 `final_decision.plan.auto_run`에도 들어간다.
  거절된 풀어 줌은 저장된 관문을 바꾸지 않으므로 `auto_run`이 없는 채로 여전히 `auto`라고 적혀 있다. B4는 `auto`만으로
  상태를 추정하지 말고 이벤트의 거절 이유를 보여 줘야 한다.
- 여전히 보수적이다. 제안 시점에 패키지 단계를 측정하지 못한 계획(다른 단계를 `requires`하는 단계는 미리보기가 미뤄진다)은
  절대 자동으로 풀리지 않고 사람에게 간다. `policy.batch.auto`가 true가 되기 전에는 꺼져 있고, 다른 자동 실행과 같은 끄는
  스위치(`GUARDIAN_AUTO_APPLY`, `GUARDIAN_UI_ACTIONS`)를 따른다.
- 리뷰 반영(보안 리뷰, 2026-10-20): 보호 검사는 단계마다 돌며, 끄는 스위치가 켜졌거나, `policy.batch.auto`가 꺼진 채 저장되었거나
  (엔진의 클라이언트는 토론 시작 시점의 정책을 쥐고 있으므로 `ProjectManager.load`로 프로젝트를 다시 읽는다),
  `plan_ops.plan_hash`가 표결된 계획과 달라졌을 때도 계획을 멈춘다. 멈춘 단계는 `STALE`, 의존하는 단계는 `SKIPPED`이지만
  이미 실행 중인 단계를 중단시키지는 않는다. `release`는 프로젝트를 지금 저장된 대로 읽는다. `_run_step`은 노드 잠금을 얻은
  뒤 저장된 행을 실행하며, 기다리는 동안 도구나 인자가 바뀐 단계는 거절한다. 명령 세트나 스크립트 단계는 확인받은
  `script_sha`를 `ops_actions.execute`의 `expected_sha`로 들고 가며, 그 뒤에 편집된 세트는 실행하지 않는다.
  `finish_auto_plan`은 `json_set` UPDATE 한 번으로 병합하고 `set_plan_gate`는 이미 시작한 계획의 관문을 덮어쓰지 않는다.
- 리뷰 반영(코드 리뷰, 2026-10-20): `release`는 지금 저장된 프로젝트로 새 `MCPClient`를 만들어, 실행이 모든 단계를 현재의
  `allowed_actions`, `deny_paths`, `max_parallel_hosts`로 검증한다(엔진의 클라이언트는 토론 시작 시점의 값을 쥐고 있다).
  계획의 프로젝트 id와 다른 프로젝트는 거절한다. 선점 뒤에 무엇이 잘못되든(데이터베이스 잠금 포함 모든 예외) 기록된다.
  `finish_auto_plan`은 `finally`에서 돌고 결과는 `error`와 함께 `started`로 나간다. `release` 자체가 예외를 내면 엔진이 저장된
  `gate.auto_run`을 읽어 선점된 시작을 시작된 것으로 보고한다. `claim_auto_plan`은 계획 해시를 받아 저장된 관문이 여전히 그
  계획에 `auto`라고 하는지 트랜잭션 안에서 다시 확인한다. 날짜와 `started_at`은 시계를 한 번만 읽어 만든다. 엔진은 자동 실행이
  시작되기 전과 끝난 뒤에 토론과 결정을 저장한다(허브 작업이 계획 도중 끊겨도 기록이 남는다). 제안 시점에 측정되지 않은
  (미뤄졌거나 실패한 미리보기) 명령 세트·스크립트 단계는 패키지 단계처럼 절대 자동으로 풀리지 않는다.
- 운영자 결정이 남은 것(보안 리뷰, 바꾸지 않음): 시스템 관리자의 `ops:package_install`은 되돌릴 수 있는 것으로 센다(postinst는
  root로 돌고 제거가 그 일을 되돌리지 않는다. npm/bun 전역 설치는 `ignore_scripts`여도 `bin`을 추가한다). 모두 기권했거나
  인용 없는 반대만 있는 계획은 풀린다(5.3). 한 단계짜리 계획으로 엔진이 계획 예산 안에서(`max_plans_per_day` x `max_steps`까지)
  중간 위험 행동을 자동 실행할 수 있다. `restart_service`는 `max_parallel_hosts`만큼의 노드에서 한꺼번에 돈다. apt 다운그레이드가
  `upgrade:`로 표시된다. 허브 감사 줄에는 행위자가 없다.
- 테스트: `test_batch_auto.py`(풀어 주는 조건, 원자적 선점, 상한, 별도 행위자, 보호)와 `test_batch_debate.py`의 엔진 케이스.

### 12.4 B4 (2026-10-20 구현, 배포 전)

- B4a, 계획 화면의 관문: `PlanGate.tsx`가 관문의 결정과 사유, 비평가별 표(사유, 인용한 점검, 조언은 조언으로 표시), 계획 해시,
  실행 기록(`auto_run`)을 보여 준다. `plan_view`의 단계에 이제 `reversible`이 들어간다. 엔진이 거절한 풀어 줌은 `_auto_run_plan`이
  `DecisionDBManager.note_release_refusal`로 `gate.release_refused`(`reasons`, `at`)에 저장하고, 화면은 그것을 읽을 뿐
  `auto`만으로 상태를 추정하지 않는다.
- B4b, 프로젝트 정책: ops 편집기에 "배치" 구역(`auto`, `max_plans_per_day`, `max_steps`, `max_parallel_hosts`)이 생겼다. 하면서 고친
  버그: 편집기가 자기 필드만으로 `policy`를 다시 만들어서 대시보드에서 프로젝트를 저장하면 `policy.watch`와 `policy.batch`가
  사라졌다. `lib/project.ts`가 이제 편집하지 않는 정책 키를 모두 보존한다.
- B4c, 명령 세트: `GET/POST /api/command-sets`, `DELETE /api/command-sets/{id}`(Go: `--command-sets`, 세트를 stdin JSON으로 받는
  `--command-set-save`, `--command-set-delete`, `--command-set-confirm`). `command_sets.save_set`은 `validate_set`으로 검증하고, `reviewed`를 오늘
  날짜로 찍으며(클라이언트가 정할 수 없다), 내장 id, 심볼릭 링크 디렉터리/파일, `MAX_FILE_BYTES`를 넘는 파일을 거절하고,
  원자적으로 쓴다(`.tmp-*.json`, 로더가 무시한다). 내장 세트는 읽기 전용이라 대화상자에서 새 id로만 저장할 수 있다. 운영자
  세트는 `<GUARDIAN_DATA_DIR>/command-sets/`에 산다. 설정 -> 명령 세트에 두 저장소가 검토 경과(`report()`의 오래됨 표시)와 함께
  나오고, 계획 화면의 즉석 `ops:run_script` 단계는 스크립트와 사유가 채워진 "명령 세트로 저장"을 제공한다. 운영자가 대화상자에서
  확인하기 전에는 아무것도 저장되지 않는다.
- 이탈: 여기서 저장한 세트는 자동으로 풀릴 수 있으므로(되돌릴 수 있고 낮은 위험, 7장) 저장은 나중에 사람 없이 돌 수 있는 코드를
  쓰는 일이다. 엔진이 `reviewed`를 찍고, 180일 뒤의 오래됨 경고는 내장 세트처럼 저장한 세트에도 적용된다.
- 리뷰 반영(코드 리뷰, 2026-10-20): `Modal`이 Escape와 Tab을 가장 안쪽 대화상자에서 멈춘다("명령 세트로 저장" 대화상자에서 Escape를
  누르면 아래의 계획까지 닫혔다). 새 세트나 내장 세트의 사본은 id가 이미 있으면 대화상자에서 거절한다(`takenIds`). 운영자 세트를
  조용히 덮어써서 대기 중인 계획을 `STALE`로 만들 수 없다. 자동 배치 실행을 켜면 하루 최대 0이 1로 올라가고(허브는 0과 `auto`를
  거절한다) 안내문도 그렇게 말한다. `PlanGate`는 결정할 단계가 남지 않으면 "풀림/대기/거절" 판정을 숨긴다(`open`). 위험도 칩은
  카탈로그 라벨을 쓴다. 대시보드에서 저장하면 `batch`가 없던 ops 프로젝트에도 `policy.batch`(허브 기본값)가 쓰인다.
- 운영자 결정(2026-10-20): 저장한 세트는 별도 확인 전까지 자동 실행을 미룬다. 대시보드에서 저장한 세트는 `risk`, `reversible`,
  `undo`를 스스로 선언하므로, 이 장치가 없으면 즉석 스크립트(그 자체로는 항상 high 위험)를 low, 되돌릴 수 있음으로 저장해
  `needs_sudo`일 때 sudo로도 자동 실행시킬 수 있었다. 구현: `command_sets.confirm_set`이 세트 본문의 sha256(`content_digest`:
  `reviewed`, `source`, `confirmed`를 뺀 모든 필드)을 운영자 디렉터리의 `.confirmed.json`(점으로 시작해 로더가 무시한다. 원자적
  쓰기, 심볼릭 링크 거절)에 저장한다. `is_confirmed`는 내장 세트면 참이고, 운영자 세트는 저장된 다이제스트가 현재 본문과 같은
  동안만 참이다. 어떤 수정이든 확인을 취소하고, 같은 내용으로 다시 저장하면(`reviewed`만 바뀜) 유지되며, 삭제하면 항목을 지우고,
  파일이 없거나 위조·손상되면 아무것도 확인하지 않는다. `plan_ops.gate_item`은 세트 단계에 `unconfirmed`를 표시하고(읽을 수 없는
  세트도 마찬가지), `batch_gate.step_problem`이 이를 거절한다. 관문에서 한 번, 실행 시점에 `step_guard`로 한 번 더 막아 사람이
  결정한다. CLI `--command-set-confirm ID`(내장 세트는 종료 코드 2, 없는 세트는 3), `POST /api/command-sets/{id}/confirm`, 설정 ->
  명령 세트의 칩("자동 실행 확인됨/미확인")과 확인 대화상자 뒤의 확인 버튼. 허브의 데이터 디렉터리에 쓸 수 있는 사람에 대한
  방어는 아니다(그 사람은 세트도 쓸 수 있다).
- 운영자 결정이 남은 것(보안 리뷰, 바꾸지 않음): (1) 확인한 세트도 `risk`, `reversible`, `undo`를 적힌 대로 믿는다. 선택지:
  `needs_sudo`이면 위험도를 medium 이상으로 하한 두기. (2) 고친 `check`는 `script_sha` 드리프트 검사가 단계를 거절하기 전에 미리보기에서 단계마다 한 번 돈다(스크립트 본문은 먼저
  검사한다). `check_sudo`인 `check`는 제안 시점에 root로 돈다. (3) `reviewed`는 저장할 때마다 찍히므로 180일 경고는 검토가 아니라
  마지막 저장을 잰다. (4) 세트의 저장·삭제는 허브 감사 로그에 남지 않는다. (5) ReDoS 방어(`NESTED_QUANTIFIER`)는 `(a|a)*$` 같은
  교대를 놓친다. (6) 저장하는 세트 수에 상한이 없다.
- 테스트: `test_command_sets.py`(`StoreWriteTests`), `test_config_ops.py`(`CommandSetConfigTests`), `debate_handler_test.go`의
  `TestCommandSetRoutes`, 웹은 `commandsets.test.tsx`, `debate.plans.test.tsx`, `projects.test.tsx`.

### 12.5 F1, 자유 스크립트 (2026-10-20 구현·배포, `6b24987`)

결정 4를 구현한다. 운영자가 설정하기 전까지 어디서나 꺼져 있다. `policy.batch.scripts` 기본값이 `"approve"`이므로 배포만으로
동작은 바뀌지 않는다.

- **정책.** `policy.batch.scripts`(`"approve"` 기본 | `"vote"`), `policy.batch.script_groups`(노드 그룹 id),
  `policy.deny_commands`(fnmatch 패턴, 명령 한 개 전체와 맞춘다). `project_registry`는 `auto` 없는 `vote`, `script_groups` 없는
  `vote`, `vote` 없는 `script_groups`, `ops:run_script`가 허용 액션가 아닌 `vote`, 대상에 속하지 않는 그룹(`check_ops_targets`)을
  거부한다.
- **어느 노드가 자유인가.** `plan_ops.free_script_hosts(project)` = 대상 ∩ `script_groups`, 허브는 절대 아니다(결정 DB와 fleet
  키가 있다). 관문이 판정할 때, 그리고 `batch_auto.step_guard`에서 각 단계 직전에 그때 저장된 프로젝트로 다시 계산하며, 못 읽으면
  자유 노드가 없는 것으로 닫는다.
- **관문.** `step_problem`은 `ops:run_script`이면서 `free_script`이고 측정된 단계만 통과시킨다(READY, 미측정, 미확인 검사가 먼저).
  그 밖의 되돌릴 수 없거나 high인 단계는 여전히 사람에게 간다. 새 조건: 자유 스크립트가 있는 계획은 **명시적 찬성 표가 최소 한
  개** 있어야 한다. 기권은 다른 모든 계획에서 찬성으로 세지만(5.3), 아무도 읽지 않은 스크립트가 그 위에서 실행되어서는 안 된다.
  2026-10-20의 규칙보다 엄격하며 운영자에게 별도 확인을 요청했다.
- **표결.** `batch_votes._step_line`이 해당 단계에 "RUNS WITHOUT A PERSON"을 붙이고, 표결 과제는 비평가에게 그 표가 스크립트에 대한
  유일한 검토임을 알린다.
- **카나리.** judge 규칙(`JUDGE_BATCH_RULES`, 프롬프트 버전 `2026-10-20.p17`)은 스크립트를 한 노드에서 먼저 실행하고 나머지는
  `requires`와 `on_failure: "stop"`으로 기다리게 하라고 한다. 관문이 강제하는 것이 아니라 프롬프트 규칙이다. 관문이 강제하는 것은
  코퍼스와 표결이 다룬다.
- **우회 형태.** `command_sets._BYPASS_PATTERNS`(`eval`, 셸로 파이프, `sh -c "$(`, `source <(`, `$(base64 -d`)는 `approve`
  모드를 포함한 **모든** 즉석 스크립트에서 거부된다(작은 동작 변경). 저장되고 확인된 세트에는 적용되지 않는다. 경보선의 값싼
  우회를 시끄럽게 만들 뿐이다. `deny_commands`는 `deny_paths`처럼 제안 시점과 검증 시점에 확인한다.
- **셸 래퍼(2026-10-22).** 리뷰에서 `sh -c '<명령>'`이 명령어 첫 단어가 `sh`라는 이유로 재난 경보선과 `deny_commands`를
  모두 피한다는 것이 확인됐다. 이제 `script_refusal`과 `deny_refusal`은 `sh|bash|dash|zsh|ksh|ash -c '...'`(`-lc`,
  `-o pipefail -c`, `$'...'`, `<<<` 포함)의 안쪽 텍스트를 꺼내 같은 검사를 다시 적용한다. 깊이는 4까지이고, 그보다 깊거나
  층이 64개를 넘으면 읽을 수 없다고 보고 거부한다. 우회 형태가 아니라 경보선이므로 저장된 세트에도 적용된다. 남은 것(경보선이지
  샌드박스가 아니라는 설계상): 명령과 동사 사이의 플래그(`docker --context x system prune`), `python -c`, `perl -e`,
  `bash <(...)`, `ssh host '...'`, 변수 간접 참조. 이들은 비평가 표결이 통제한다.
- **judge와 프로젝트.** 기본 제공 `fleet` 프로젝트, `fleet-provision` 팀, `ops_provision_judge` 페르소나가 이제 `ops:run_command_set`와
  `ops:run_script`를 가진다. judge 프롬프트는 패키지 외 작업을 건드리지 말라는 문장을 없애고, 그런 작업을 세트나 짧은 스크립트로
  계획하되 점검한 상태를 가드로 쓰고 미리 막지 말라고 한다(프로젝트와 비평가가 결정한다). 부수 효과: 배치 도구를 허용하는 모든
  프로젝트에서 `votes_enabled`가 true이므로 `fleet`에서도 judge가 계획을 쓰면 표결이 돈다. `fleet-ops` 프롬프트는 바이트 단위로
  그대로다.
- **대시보드.** 프로젝트 편집기에 "추가로 막을 명령", 배치 블록에 표결 체크박스(자동 실행을 켜기 전까지 비활성)와 그룹 목록. 두
  카탈로그 모두.
- **코퍼스.** `batch_corpus.py`에 `freescript` 클래스(결함 6: 그룹 밖, 한 단계만 밖, 인정된 반대, 찬성 없음, 조언뿐, `auto` 꺼짐)와
  정상 계획 2(둘 다 찬성, 한 명 기권)를 추가: 정상 14, 결함 35, recall 1.00, 오차단 0.00(`--targets`).
- **테스트.** `test_free_scripts.py`(해석, 관문, 가드, 우회 형태, 거부 목록, 실행), `test_provisioning.py`(도구 관문, 배치 도구를 가진
  judge만 배치 규칙을 읽는다), `test_batch_corpus.py`, `projects.test.tsx`.
- **하지 않은 것.** F2(스크립트용 읽기 전용 탐침). 아직 어떤 프로젝트에도 `script_groups`가 없다. 운영자가 고른다.

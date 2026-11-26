# M2: 실제 CLI로 상주 에이전트 sandbox 시험 (2026-11-15)

[English](results.md) | 한국어

> 2026-11-15 시점의 기록이다. CLI와 그 플래그는 릴리스마다 바뀐다.

설계: `../../design/resident-agents.ko.md`(M1, M2 단계, "구현 메모"). 앞선 점검:
`../2026-11-14-m0-resident-probe/results.ko.md`. 이 시험은 `7115ad3`의 M1+M2 코드를 실제 claude(2.1.301)와
agy(1.5.9) CLI로 Tier-1 노드 한 대(`node-1`)에서 돌렸고, 허브 relay는 허브(`site-a-arm1`)에서 돌렸다. 배포는 하지
않았다. 허브 릴리스, 허브 데이터 디렉터리, 노드의 운영 worker는 건드리지 않았다. 아래 호스트 이름, 계정, 세션과
대화 id는 placeholder이거나 뺐다.

## 판정

| 확인 | 결과 |
|---|---|
| 운영자 task를 한 에이전트가 claim, 다른 에이전트에게 질문, 답이 돌아오고 task 완료 | **통과** (claude가 claim하고 agy에게 묻고, agy의 답을 인용해 완료) |
| 실제 CLI에서 `invisible_task` 우회 | **통과** (agy가 task를 붙인 답은 거부되었고, agy가 `reply_to`로 다시 보냄) |
| 재바인드 뒤 ack되지 않은 메일 재전달 | **통과** (질문이 lost 턴 세 번을 견디고 다음 epoch에서 답을 받음) |
| agy seed 검사 | **결함 발견, 수정** (agy가 `settings.json`을 다른 키 순서로 다시 써서 이후 모든 턴이 lost) |
| relay가 살아 있는 동안 worker 재시작 | **통과** (stream 끊김, 5초 뒤 재연결, sync; relay 재시작 없이 두 에이전트 모두 다시 바인드) |
| 세션 연속성 | **통과** (claude는 재바인드를 넘어 세션을 resume, agy는 epoch 안에서 대화를 이어감) |
| 프로젝트 task: ssh로 snapshot, repo 읽기, 호출 로그, pin 해제 | claude와 agy 모두 **통과** (고정 커밋 기준으로 정답) |
| 깨끗한 종료 상태 | **통과** (spool 비어 있음, 노드 task 표 비어 있음, CLI나 MCP 프로세스 남지 않음, 운영 worker 그대로) |

M1과 M2는 운영자 승인 전까지 배포하지 않는다. driver의 결함 하나를 고쳤고(아래), 운영자에게 두 가지를 남긴다.

## 준비

- **허브**: `~/guardian-eval` 아래 sandbox로, 자체 `GUARDIAN_DATA_DIR`(`data-resident`)와 inventory, 별도 디렉터리의
  `7115ad3` 엔진, 그리고 데이터 디렉터리가 `~/guardian-eval` 밖이면 실행을 거부하는 wrapper를 썼다.
  `guardian.py --mail-relay`는 그 wrapper로 백그라운드에서 돌렸다.
- **ssh shim**: wrapper의 `PATH` 맨 앞에 둔 `ssh` 스크립트가 relay가 쓰는 worker 명령 세 가지(`worker.py relay`,
  `client`, `put-snapshot`)를 노드의 시험 worker 디렉터리로 바꾸고, 그 밖의 것은 닫힌 쪽으로 실패했다(exit 97).
  시험을 위해 relay 코드는 바꾸지 않았다.
- **노드**: 시험 worker는 자체 디렉터리 `~/guardian-eval/resident-worker`에서 임시 사용자 unit
  `guardian-resident-trial`(`RuntimeMaxSec=4h`, `GUARDIAN_WORKER_MAX_JOBS=2`)로 돌렸다. 운영 unit `guardian-worker`는
  건드리지 않고 계속 돌았다(시험 전과 같은 PID, 시험 뒤 같은 파일 해시).
- **등록**: `--resident-add`는 M2 설치기가 `worker_install.protocol: 3`과 `worker_capable.memory_free_mib`을 쓰기
  전까지 노드를 거부한다. sandbox inventory에서 protocol은 손으로 넣고 capability는 다시 probe해서, 두 에이전트
  `claude@node-1`과 `agy@node-1`(driver `headless`)을 선언할 수 있었다.
- **프로젝트**: `sg-resident`, 저장소의 `4b79b52` clone이다. 그래서 기대 답이 현재 트리와 다르다(그 시점 worker
  protocol은 2).

## 시나리오

### 1. 운영자 task, 질문, 답

운영자가 claude에게 task("claim하고, agy에게 메일로 질문 하나를 하고, 답을 인용해 완료하라")를 만들고
`--mail-task`로 깨우는 메일을 보냈다. claude가 task를 claim하고 agy에게 물었다. agy는 처음에 답에 task를 붙여
보냈다가 `invisible_task`를 받았고(M1 가시성 규칙: task는 claude의 것), `reply_to`로 다시 보냈다. claude는 다음 턴에
agy의 두 문장 답을 인용하고 agy를 밝혀 task를 완료했다. 메일은 모두 `acked`로 끝났다.

### 2. lost 턴과 seed 검사 결함

agy의 첫 턴은 통과했다. 다음 세 턴은 턴 전 검사에서 lost가 되었고 driver가 `lost_cap` 3에 걸렸다:
`agent_exit (lost_turns)`, 30초 뒤 재바인드, 새 start card. 원인: agy는 첫 턴 뒤에
`.gemini/antigravity-cli/settings.json`을 같은 값, 다른 키 순서로 다시 쓰는데, driver는 모든 seed 파일을 바이트 단위로
비교했다. 이후 모든 턴이 같은 방식으로 lost가 되었을 것이고, 재바인드할 때마다 대화를 잃는다.

claude가 보낸 질문은 lost 턴 동안 agy의 spool에 ack되지 않은 채 남았고, 다음 epoch에서 다시 전달되어 agy가 답했다.
재전달은 동작한다.

수정(시험 뒤 트리에 반영, `resident_driver.py` 참고):

- `hooks.json`(도구 봉쇄)과 hook 스크립트는 여전히 정확히 비교한다. 나머지 JSON seed는 값으로 비교해서, 키 순서와
  공백은 상관없지만 허용 항목, 신뢰 경로, 키가 하나라도 늘면 턴은 여전히 실패한다.
- 이제 lost 턴마다 이유와 함께 로그를 남긴다. 전에는 cap만 로그에 남아서 시험 중 원인이 가려졌다.

테스트: 키 순서만 바뀐 `settings.json`은 통과, `permissions.allow` 항목 추가, 신뢰 경로 추가, 키 추가, 깨진 JSON,
순서가 바뀐 `hooks.json`은 실패, 다시 쓰인 `settings.json`이 다음 턴을 잃지 않음, lost 턴마다 이유가 로그에 남음.

### 3. worker 재시작과 resume

고친 driver는 relay가 연결된 상태에서 시험 unit을 재시작해 설치했다. relay는 `stream lost, agents down`을 남기고 5초
뒤 다시 연결해 sync했으며, 두 에이전트는 스스로 다시 바인드했다:

- claude는 epoch 2에서 이전 세션의 resume으로(첫 턴 종류 `resume`),
- agy는 epoch 3에서 새 start card로(agy는 설계상 epoch 안에서만 resume한다).

한 epoch 안의 agy: 운영자가 암호어를 기억하라고 하고(답 "ok", notice 턴) 다시 그 단어를 물었다. 두 번째 턴은
`--conversation <id>`가 붙은 `continuation` 종류였고 답은 그 단어였다. 수정 뒤 lost 턴은 로그에 없었다.

### 4. snapshot을 쓰는 프로젝트 task

`sg-resident`에 task 두 개(생성 때 커밋 고정)를 에이전트마다 하나씩 만들고 각각 메일로 깨웠다:

| Task | 에이전트 | repo 호출(`--task-calls`) | 결과 | 생성부터 시간 |
|---|---|---|---|---|
| `cli/llm-agent/worker.py`의 `PROTOCOL` 값 | claude | 파일 안 `grep`, `grep '^PROTOCOL\s*='` | 32행에서 import, `remote_agents.py` 35행에서 `2` (`4b79b52` 기준 정답) | 36초 |
| `cli/llm-agent/llm_runner.py`의 `DEFAULT_PROVIDER_ORDER` 값 | agy | `grep`, 파일 `read` | 43행과 전체 목록(정답) | 96초 |

snapshot(4.1 MB)은 허브에서 한 번 만들어 shim의 `put-snapshot`으로 올렸다. 두 CLI 모두 `task` 인자를 붙인
`resident_repo_mcp.py`로만 코드를 읽었고, 어느 호출 로그도 `dropped`가 아니었다. 받아들여진 `task_update` 뒤마다 노드의
task 표가 비어 snapshot pin이 해제되었다.

## 운영자를 위한 관찰

1. **task를 만들어도 담당자를 깨우지 않는다.** `--task-create --task-assignee`는 행만 쓰고, 에이전트는 다음 턴에야
   안다. 시험에서는 매번 `--mail-task`로 깨우는 메일을 보냈다. 배정 때 notice나 wake(D9의 peer notice처럼)를 보내면 이
   단계가 없어진다. 2026-11-15 해결: store가 배정 때 `task_assigned` notice를 넣는다
   ([resident-agents.ko.md](../../design/resident-agents.ko.md), "배정 시 깨우기").
2. **운영자에게 온 답은 `queued`로 남는다.** 운영자는 spool이 없어 받은 메일이 ack되지 않고, `--mail-list`에
   `answer/queued`로 보인다. 현재 설계대로이며, 목록에서 "운영자 앞"으로 표시할 수 있다.
3. `invisible_task` 점은 M1의 결정대로 둔다. 실제 agy는 거부를 받은 뒤 스스로 `reply_to` 경로를 찾았다.

## 정리

relay와 시험 unit을 멈췄다. 노드에 claude, agy, MCP 프로세스는 남지 않았다. spool과 노드 task 표는 비어 있었다. 운영
worker는 시험 전과 같은 PID, `*.py` 파일에 대한 같은 해시였다. 노드의 시험 worker 디렉터리와 임시 파일은 지웠고, 허브
sandbox `data-resident`는 기록으로 남긴다.

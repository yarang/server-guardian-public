---
name: guardian-fleet-ops
description: >-
  Fleet-wide node operations through Server Guardian on the hub: check every fleet node, start an ops debate with the
  right team (cleanup, services, tailnet report, package install and removal), follow it, and show or run the
  proposed actions or plans once the operator approves. Use it on the hub when the operator asks about the whole fleet
  or several nodes ("전체 노드 점검", "플릿 정리", "운영 토론 시작", "서비스 재시작", "패키지 설치", "제안된 액션 보여줘",
  "계획 실행"). Never run cleanup, install or restart commands on other nodes directly.
---

# Guardian fleet operations (hub only)

Guardian is the only way to change other nodes: its actions are validated against the project's targets and deny
rules, measured before and after, refused when the target changed, held per node, and written to the audit log. Do
not ssh to fleet nodes to delete or restart anything yourself.

## Setup (each shell)

```bash
cd ~/.guardian/current/cli/llm-agent   # the release the hub runs
set -a; . ~/.guardian/agent.env; set +a   # AGENT_TOKEN and the inventory path; never print the token
API=http://127.0.0.1:9870
auth=(-H "Authorization: Bearer $AGENT_TOKEN")
```

## 1. Look before acting

- Thresholds across the fleet (read-only, starts nothing): `python3 guardian.py --ops-watch --dry-run`
- Running debates: `curl -s "${auth[@]}" $API/api/debates | python3 -m json.tool | head -40`
  Do not start a second debate on the same nodes while one runs.

## 2. Start an ops debate (proposals only)

Write the task in Korean, name the nodes and the goal, and pick the team by what the order is about:

| Order | team | Result |
|---|---|---|
| disk, journal, caches, old kernels, logs (the default; also what the nightly watch uses) | `fleet-ops` | actions |
| a failed or unstable service or container, a restart | `fleet-services` | actions (`ops:restart_service`) |
| headscale or tailnet nodes (a report and the steps a person takes) | `fleet-mesh` | report only, no actions |
| installing or removing packages, a short script or command set on several nodes | `fleet-provision` | a plan |

Leave `team` out for `fleet-ops`; otherwise add it to the request body (`"team": "fleet-provision"`).

```bash
python3 - <<'PY'
import json, os, urllib.request
task = "<대상 노드>의 <문제>를 점검하고 안전하게 정리할 액션을 제안하라. 다른 노드는 건드리지 마라."
req = urllib.request.Request("http://127.0.0.1:9870/api/debates", method="POST",
    data=json.dumps({"task": task, "project": "fleet"}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AGENT_TOKEN"]})
print(json.load(urllib.request.urlopen(req, timeout=30))["id"])
PY
```

## 3. Follow it

- Status: `curl -s "${auth[@]}" $API/api/debates/<job id> | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'])"`
- Progress: `~/.guardian/data/debate-jobs/<job id>/events.jsonl` (checks: `retrieval_served` with `via: ops`;
  verdict: `jev_gate`, `debate_finished`; proposals: `action_proposed`). The thread id is in `result.json`.

## 4. Actions

- List: `python3 guardian.py --actions <thread id>`; one in full (arguments, risk, preview of what it would delete):
  `python3 guardian.py --show-action <id>`
- Run ONLY after the operator says yes to that action id in this conversation:
  `python3 guardian.py --run-action <id> --yes --actor <operator name>`
  Report the result's verify block (before, after, freed). A `STALE` action means the node changed: debate again.
- Reject on request: `python3 guardian.py --reject-action <id> --actor <operator name>`

## 5. Plans (`fleet-provision`)

A `fleet-provision` debate ends in a plan (steps by host, with `requires` and `on_failure`), not in actions.

- List: `python3 guardian.py --plans <thread id>`; one with its steps by host: `python3 guardian.py --plan-show <id>`
- Show the operator every step (host, tool, packages or script) and the gate's votes and reasons before asking.
- Run ONLY after the operator says yes to that plan id in this conversation:
  `python3 guardian.py --plan-run <id> --yes --actor <operator name>`. Report each step's state; a step refused as
  `STALE` means the host changed: debate again. Reject on request: `python3 guardian.py --plan-reject <id> --actor <operator name>`
- A project that opted in to automatic batches (`policy.batch.auto`) may start a plan on its own after the critics'
  vote; then report what ran, do not run it again.

## Never

- Edit files under `~/.guardian/data` directly, or change project, team or policy files without the operator asking.
- Run an action the operator did not approve, or several at once on "do them all" without listing them first.
- Start a deploy or restart `server-guardian-agent`; deploys come from the admin Mac (`scripts/install-agent-service.sh`).

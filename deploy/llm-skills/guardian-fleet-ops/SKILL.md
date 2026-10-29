---
name: guardian-fleet-ops
description: >-
  Fleet-wide node operations through Server Guardian on the hub: check every fleet node, start an ops debate that
  proposes cleanup actions, follow it, and show or run the proposed actions once the operator approves. Use it on the
  hub when the operator asks about the whole fleet or several nodes ("전체 노드 점검", "플릿 정리", "운영 토론 시작",
  "제안된 액션 보여줘", "액션 실행"). Never run cleanup commands on other nodes directly.
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

Write the task in Korean, name the nodes and the goal, and leave the team to the project (fleet-ops):

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

## Never

- Edit files under `~/.guardian/data` directly, or change project, team or policy files without the operator asking.
- Run an action the operator did not approve, or several at once on "do them all" without listing them first.
- Start a deploy or restart `server-guardian-agent`; deploys come from the admin Mac (`scripts/install-agent-service.sh`).

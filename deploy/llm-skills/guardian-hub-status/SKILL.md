---
name: guardian-hub-status
description: >-
  Read-only status of the Server Guardian hub itself: the hub agent service, its timers (DB backup, nightly ops watch), the
  recent audit log, running debates and the data directory. Use it on the hub when the operator asks whether Guardian
  is healthy or what it did recently ("가디언 상태", "허브 상태", "최근 실행 기록", "야간 점검 결과").
---

# Guardian hub status (read-only)

Report; change nothing. Do not restart services, edit files or print secrets (agent.env, llm.env, keys).

```bash
systemctl is-active server-guardian-agent guardian-fleet-agent
systemctl list-timers server-guardian-db-backup.timer server-guardian-ops-watch.timer --no-pager
tail -n 3 ~/.guardian/agent.log                                  # version and listen addresses
journalctl -u server-guardian-ops-watch --since -2d --no-pager | tail -n 30   # last nightly watch
du -sh ~/.guardian/data 2>/dev/null; df -h ~ | tail -n 1
```

Recent commands the hub ran (audit log, newest last; `source` tells a check, an action or the dashboard apart):

```bash
tail -n 30 ~/.guardian/data/audit/exec.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    print(e['ts'], e.get('source') or 'dashboard', e.get('op') or '', e.get('action_id') or '', e['status'], ','.join(e['targets'])[:60])"
```

Running and recent debates:

```bash
set -a; . ~/.guardian/agent.env; set +a
curl -s -H "Authorization: Bearer $AGENT_TOKEN" http://127.0.0.1:9870/api/debates | python3 -c "
import json, sys
for d in (json.load(sys.stdin).get('debates') or [])[:8]:
    print(d['id'], d['status'], d.get('project'), d['task'][:70])"
```

Summarize: the hub agent up or not and its version, last backup and watch runs, failures in the audit log, running debates.

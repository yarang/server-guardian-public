---
name: guardian-ask
description: >-
  Quick, read-only question about a Server Guardian project's code (or an ops project's servers), answered by one
  Guardian agent with checked citations and no debate. Use it on the hub when the operator asks where something is or
  what it does ("X 어디 있어", "이 함수 뭐 하는 거야", "재시도 로직 설명해줘", "코드에서 찾아줘"). The answer is labelled
  SOLO: unverified, no Judge. For anything that must be decided or changed, start a debate instead.
---

# Guardian ask (hub only, read-only)

One agent reads the repository and answers. There is no Judge, no round and no approval, and nothing it says can be
run: it proposes no patch or action. Use `guardian-fleet-ops` when something should be done, and `guardian-host-check`
for the node you are on.

## Run

```bash
cd ~/.guardian/current/cli/llm-agent   # the release the hub runs
python3 guardian.py --agent-run "<question in the operator's words>" --project <id>
```

- `--project` defaults to `server-guardian`. `--context-path 'apps/agent/*.go'` (repeatable) narrows what the agent
  reads first; `--role <persona id>` picks another persona (a Judge role is refused); `--provider claude|codex|agy`
  pins one provider; `--language English` changes the answer language (Korean by default); `--json` prints the stored record.
- Two questions run at once at most (`GUARDIAN_MAX_SOLO`); exit code 4 means both slots are busy, wait and retry.

## Report

- Print the first line as it is: `SOLO answer (unverified, no Judge)`. Never call the answer verified, approved or
  decided.
- Keep the citation line (`N verified, N unshown, N missing`). Say so when it lists `unshown` or `missing` citations:
  those lines were not shown to the agent or do not exist, so do not rely on them.
- `DEGRADED` in the header means no provider answered and the text is a canned fallback: say that, do not summarise it.
- If the operator wants a second opinion, start a debate with the question and the answer as reference text.

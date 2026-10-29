# z.ai provider (GLM Coding Plan) for the debate engine (design)

English | [한국어](zai-provider.ko.md)

Status: decided 2026-10-23 by the operator, built; enabled 2026-10-25: the key is on the hub and on the six non-hub Tier-1 hosts, and workers on those six carry the new `llm_runner.py`. Without `ZAI_API_KEY` the provider is never ready.
Origin: the operator holds a z.ai GLM Coding Plan API key and wants GLM models as debate agents.

## Decisions (operator, 2026-10-23)

1. The key is a Coding Plan key. Its terms limit use to supported coding tools; the operator accepted that the engine
   drives the `claude` CLI automatically.
2. `zai` is part of the automatic fallback chain.
3. The key is deployed to the Tier-1 worker hosts as well as the hub.

## Design

`zai` is a provider of its own that runs the **claude binary** under a separate identity: z.ai's Anthropic-compatible
endpoint, its own key, its own config directory. Nothing else is installed.

| Piece | Rule |
| --- | --- |
| Order | `claude, codex, agy, zai, opencode, ollama, llm`. Teams can also pick it per member: `"providers": ["zai"]`. |
| Ready | The `claude` binary is on PATH **and** `ZAI_API_KEY` is set. A claude login alone does not make `zai` available. |
| Credentials | `env_policy`: `ZAI_*` reaches only `zai`; plain claude never sees it. A zai run gets no `CLAUDE_CODE_OAUTH_TOKEN` and no `ANTHROPIC_API_KEY`. A missing key fails the call (`RuntimeError`), it never borrows the claude login. |
| Mapping | `ZAI_API_KEY` -> `ANTHROPIC_AUTH_TOKEN`; `ZAI_BASE_URL` (default `https://api.z.ai/api/anthropic`) -> `ANTHROPIC_BASE_URL`; `ZAI_MODEL` and `ZAI_{OPUS,SONNET,HAIKU}_MODEL` -> `ANTHROPIC_DEFAULT_*_MODEL` (unset: the server chooses). The `ZAI_*` names are removed from the child. |
| Config dir | `~/.guardian/claude-zai` (`GUARDIAN_ZAI_CONFIG_DIR`), mode 0700, with `.claude.json` `{"hasCompletedOnboarding": true}`. Stable, because sessions are resumed from it; separate, because recent claude prefers `~/.claude/.credentials.json` over env tokens. |
| Sessions | Same flags and parsing as claude (`_session_claude(provider="zai")`), `--resume`, `--append-system-prompt`, repo MCP. Results are labelled `zai-cli`, failures `zai:`. |
| tmux | `session_ops.shell_with_llm_env(command, provider="zai")` unsets the claude login and exports `ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY"` as a shell expansion: the key is never written into the tmux command. |
| Audit | `audit_run.AUDIT_PROVIDERS` stays `["claude"]`. |

Code: `llm_runner.py` (`PROVIDER_BINARIES`, `zai_claude_env`, `_zai_process_config`, `_claude_family_config`),
`env_policy.py`, `session_ops.py`, `debate_engine.py` (standing instructions also for zai). Workers ship `llm_runner.py`
and `env_policy.py`, so they need no new file. Tests: `cli/llm-agent/test_zai_provider.py`.

## Operations

- Put `ZAI_API_KEY=...` (optionally `ZAI_BASE_URL`, `ZAI_MODEL`) into the local `~/.config/server-guardian/llm.env`
  (mode 600), then `scripts/deploy-llm-env.sh <hub> <tier-1 hosts>`. Hosts must be named; the key is never printed.
- `scripts/lib/llm-check.sh` adds a `zai` PONG line (skipped when the host has no key), so `probe-worker.sh` sees it.

## Enabled 2026-10-25

- After the operator replaced the key, a direct call answered for `glm-4.6`, `glm-4.5-air`, and the `claude` CLI answered PONG. Hub sandbox check (`~/guardian-eval/zai-verify`): PONG, `--resume` (MANGO), repo MCP tool call (`repo_grep`) all passed. With no `ZAI_MODEL` the claude CLI sends its default name `claude-opus-5-5`; z.ai answers with a GLM model (it names itself GLM) but the response labels the model `claude-opus-5-5`, so stream-json cannot tell which GLM answered. Set `ZAI_MODEL` when the model matters.
- The key was merged into each host's `~/.guardian/llm.env` (other keys kept; backup `llm.env.bak-zai`). `deploy-llm-env.sh` was not used for this, because it replaces the whole file.
- `claude` against z.ai prints a notice ("auto mode ... api.z.ai isn't compatible") before the answer. `llm-check.sh` now judges the whole output and cuts only what it shows; before, the notice pushed PONG past the 160-character cut and every host failed the zai line. Per-host checks: hub and the six non-hub Tier-1 hosts all pass.
- Models pinned by the operator on 2026-10-25, merged into the `llm.env` of the hub and the six non-hub Tier-1 hosts (backup `llm.env.bak-glm`): `ZAI_OPUS_MODEL=glm-5.3`, `ZAI_SONNET_MODEL=glm-5.2`, `ZAI_HAIKU_MODEL=glm-5.3-flash`. The plan also serves glm-4.5 to 5.1 and 5-turbo; `glm-5.3-flashx` and `glm-4.7-flash` are refused (errors 1311, 1314). z.ai has no graded think levels: thinking is on (default) or off.

## Open

- Hub sandbox check 2026-10-23 (`~/guardian-eval/zai-verify`): runner, env isolation and key handling behaved as designed; a direct call to the endpoint answered HTTP 429 `[1113] Insufficient balance or no resource package` for `glm-4.6` and `glm-4.5-air`, and the `claude` CLI retried until the 120 s limit. Re-run once the plan is active.
- Measure how long `claude` retries on a 429 from z.ai: an exhausted `zai` in the fallback chain would cost a whole timeout before the next provider (consider a short timeout for `zai` only).

- Verify on the hub sandbox `~/guardian-eval` (never production data): the PONG shows a GLM model; a session turn with
  repo MCP attached really calls `mcp__repo__*` tools under `--tools ""`; the fresh config dir needs no onboarding step
  beyond the marker file.
- Coding Plan terms for automated use, and its rate limits when the fallback chain reaches `zai`.

## 구현 메모

Implemented as designed. `installed_providers` lists `zai` only with a key, so `claude` and `zai` are independent
entries although they share a binary. Not deployed.

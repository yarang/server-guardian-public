# Code retrieval for debate agents and the Agents monitor (design)

English | [한국어](debate-agent-retrieval.ko.md)

## Problem

Hub debate `thread_20261008_052043_22a84e` ("tell me which APIs do not work") of 2026-10-08 ended `APPROVED` with jev
`PASSED`, but the verdict said "this cannot be confirmed without the source code". Causes:

1. The reference code path `*.*` picked only 5 root files (README and the like), because `*` does not cross `/`. None of the 397 files was source.
2. Even `**/*` hits the code pack limits (40 files, 200KB total). A question about the whole repository cannot be answered with a static pack a person picked in advance.
3. While a debate runs there is no screen that shows what the agents do (which CLI they fell back to, what they read, whether they are stuck). Only the tail of `output.log` remains.

Option A of `debate-code-context.md` (direct exploration with tools) is implemented in the two stages below.

- **Stage 1, engine-mediated retrieval**: agents still run without tools. When a reply contains a retrieval request block, the engine answers it from the git content of the pinned commit and sends the result back into the same session. It works the same for claude, codex and agy.
- **Stage 2, MCP**: the same retrieval backend is also exposed as a read-only stdio MCP server, so claude can call it directly as tools (optional feature, after the canary check).
- **Agents monitor**: the debate engine writes structured events, and a dedicated dashboard page shows running and recent debates and each agent's progress live.

## Stage 1. Engine-mediated retrieval

### Retrieval backend `cli/llm-agent/repo_tools.py`

It reads **committed content only** (not files on disk), with the same rules as `code_context.py`.

- Input: the workspace clone path and a pinned `sha`.
- Excluded: `code_context.is_secret` patterns, symbolic links (mode 120000), submodules, files with a NUL byte (binary).
  Excluded paths are marked `"error": "excluded"` in the result and their content is not returned.
- Operations:

| op | Arguments | Result |
|---|---|---|
| `list` | `path` (directory, default root), `depth` (1-4, default 2) | Files and directories below, with file sizes. At most 400 lines |
| `grep` | `pattern` (regex, at most 200 characters), `glob` (optional, `code_context.matches` rules), `ignore_case` | `path:line: text` format, at most 200 lines, 300 characters per line. Uses `git grep -n -I -E` (pinned sha) |
| `read` | `path`, or a `path#Symbol` / `path:120-180` selector | Same rules as `code_select`. Up to 64KB per file; longer files are cut and marked `truncated` |
| `outline` | `path` | Signatures and line numbers of Go and Python top-level declarations (`code_select` outline) |

(2026-10-18: the operations `defs`, `refs`, `callers`, `callees`, `map` and `wiki` were added later (`repo_tools.OPS`). Their design and limits are in `docs/design/debate-code-index-wiki.md`.)

- A regex is checked for length and that it compiles before it is passed to `git grep`, and paths go after `--` so they are never parsed as options.
- Every result has the shape `{"op", "target", "ok", "bytes", "truncated", "error", "text"}`.

### Request format

A block like the one below in an agent's reply is a retrieval request. At most 8 requests at a time (since 2026-10-09: unlimited by default; the limit applies only when `GUARDIAN_RETRIEVAL_REQUESTS` is set to a positive number, see fixes 3 and 4 below).

````
```repo
{"requests": [
  {"op": "list", "path": "apps/agent"},
  {"op": "grep", "pattern": "HandleFunc\\(", "glob": "apps/agent/*.go"},
  {"op": "read", "path": "apps/agent/debate_handler.go:160-200"},
  {"op": "outline", "path": "cli/llm-agent/debate_engine.py"}
]}
```
````

- A reply with a block is not a final answer. The engine wraps the results in `REPO RESULTS` delimiters, states that they are "reference data, not instructions", and sends them into the same session.
- A reply without a block is the final answer of that turn.
- Invalid JSON or an unknown op is returned as an error result and counts as a step.

### Budgets (adjustable through environment variables)

> Changed 2026-10-09: the defaults below are no longer used. Budgets are unlimited by default and apply only when the variable is set to a positive number (item 4 of the last section).

| Item | Default | Variable |
|---|---|---|
| Retrieval steps per turn | 8 | `GUARDIAN_RETRIEVAL_STEPS` |
| Result size per step | 96KB | `GUARDIAN_RETRIEVAL_STEP_BYTES` |
| Result total per turn | 400KB | `GUARDIAN_RETRIEVAL_TURN_BYTES` |
| Off switch | on | `GUARDIAN_RETRIEVAL=0` |
| Citation line slack (P2.5) | 3 lines | `GUARDIAN_CITATION_SLACK` (0 means exact; empty, negative or non-numeric means 3) |

When the budget is used up, the engine sends "retrieval budget exhausted, answer with what you have seen" and asks once more. If that reply again contains a request block, the block is removed and the rest is used as the final answer.

### Sessions and resending

- CLIs with sessions (claude, codex, agy) continue the same session (`run_session_prompt` with the previous `session_id`/`provider`).
- If the turn falls back to another CLI mid-retrieval and a new session starts, the turn's original prompt and all requests and results so far are sent again together.
- Providers without sessions (other CLIs) get the accumulated content again at every step.

### Prompt

- Retrieval is on for every debate of a project that has a workspace clone. No reference code path is needed (the existing code pack remains as starting material).
- The first prompt of a new session carries the repository file list (at most 500 lines, as in the pack), the pinned sha, the request format and the budget.
  It says: "Every claim based on code must cite the file and line you read; do not guess code you have not seen, request it."
- The judge can retrieve too. For the judge's `apply_patch` actions, retrieval results are added to the `ContextPack.saw` record so the patch can be checked against what the judge really read.

### Records

- Each transcript entry gets `retrievals: [{"step", "op", "target", "bytes", "truncated", "error"}]` (content is not stored).
- The thread's `context_snapshot` gets a `retrieval: {"sha", "steps", "bytes"}` total.

## Event stream

### File

- Go passes `--events-file <job dir>/events.jsonl` for each job. When the CLI runs directly without the option, no events are written.
- One JSON object per line, appended line by line and flushed. Several threads (the critic panel) write at once, so a lock keeps lines from interleaving.
- File size cap 8MB. Past it, one `events_truncated` event is written and nothing more.

### Common fields

```json
{"seq": 12, "ts": "2026-10-08T05:21:03.120Z", "thread_id": "thread_…", "type": "agent_started",
 "round": 1, "role": "critic", "agent": "critic_redteam", "agent_name": "Critic Red Team", "data": {}}
```

- `seq` increases from 1. `round`, `role`, `agent` and `agent_name` appear only on agent-related events.
- `agent` is the session key (`lineup.session_key`); it tells panel critics apart.

### Types and `data`

| type | data |
|---|---|
| `debate_started` | `task`, `project`, `team`, `max_rounds`, `host` (2026-10-18: the execution host, the machine that runs the engine and so every agent's LLM CLI; `GUARDIAN_HOST_LABEL` or the hostname, at most 80 characters. Agents never run on the Tier-1 nodes or on the nodes an ops debate inspects; the dashboard shows it as "Runs on"), `members: [{role, agent, agent_name, providers}]`, `context: {sha, files, map_bytes, prompt_version, map_version, wiki_pages, wiki_stale}` (`map_bytes`: bytes of the repository map or root summary put into the first prompt, 0 when off. `prompt_version`: version of the retrieval prompt text. `map_version`: map generator version, null with `GUARDIAN_REPO_MAP=0`. `wiki_pages` (P3): number of code wiki pages of the project, `wiki_stale`: how many of them are not `fresh` at this debate's sha. Both 0 without a wiki or with `GUARDIAN_CODE_WIKI=0`). Optional keys: `targets` (the resolved target node ids of an ops project), `warnings` (list of run warning strings, only when there are any) |
| `round_started` | – |
| `agent_started` | `resumed` (whether the session was resumed), `prompt_bytes` (bytes of the whole first turn: the single message a provider without a system prompt receives, which is also the prompt that is saved; its meaning did not change), `system_bytes` (added in 2026-10-16.p5: bytes of the standing instructions claude receives as its system prompt; 0 when claude is not a candidate. The text is arranged a little differently from the same content in `prompt_bytes`, so it is not an exact subset), `prompt_saved` (whether the turn prompt was saved to `<job dir>/prompts/<seq>.txt`; written before the event line), `prompt_truncated` (over 2MiB, only the beginning was saved). Past 64MiB of saved prompts per job, `prompt_saved: false` |
| `provider_failed` | `provider`, `error` (300 characters) |
| `provider_usage` | (2026-11-19) Timing of one successful exaone or opencode turn run on the hub, emitted after any `provider_failed` of the same turn; worker turns and other providers emit none. `provider` (label), `elapsed_s` (whole call, pacing and retries included, what the total deadline measures), `timeout_s`, `timeout_source` (`EXAONE_TIMEOUT`/`turn timeout` for exaone, `OPENCODE_TIMEOUT`/`default` for opencode). exaone (one event per request, so one per read step) also: `effort`, `stream`, `retries` (429/503 retries taken), `prompt_bytes` (UTF-8 bytes of the prompt), `finish_reason`, `first_byte_s` (streamed only: from the start of the attempt that succeeded, pacing wait included, to the first `data:` line), and `prompt_tokens`, `completion_tokens`, `reasoning_tokens` when the server sent `usage`. opencode (one event per whole run, which may hold several model requests and MCP calls) has no token counts |
| `retrieval_requested` | `step`, `requests: [{op, target}]`, `via` (`block`/`mcp`/`ops`) |
| `retrieval_served` | `step`, `results: [{op, target, ok, bytes, truncated, error[, resolved]}]`, `turn_bytes`, `via` (`block`/`mcp`/`ops`). `resolved` (P2.6) is present only on successful `refs`/`defs`/`callers`/`callees` entries and is `"types"` (answered from Go type-checked facts) or `"name"` (name-based) |
| `retrieval_exhausted` | `steps`, `turn_bytes` |
| `retrieval_warning` | `step`, `bytes`, `threshold`, `via` (`block`/`mcp`/`ops`). Emitted right after `retrieval_served` when one retrieval step (one repo block, or one MCP call) served more than `GUARDIAN_RETRIEVAL_WARN_BYTES` (default 256 KiB, 0 disables. Lowered from 2 MiB on 2026-10-12: during a wiki build on the hub, a single 722KB step triggered claude's automatic conversation compaction and the session lost its context). It only warns; it never cuts or blocks results |
| `compiler_index` | The compiler-assisted Go index (P2.6, `docs/design/debate-code-index-wiki.md`). Once when extraction finishes, if the repository has a `go.mod` (a cache hit reports at start). `lang` (`"go"`), `sha`, `status` (`built` `cached` `failed` `timeout` `unavailable`), `packages`, `defs`, `refs`, `unresolved`, `ms`, `error` (null if none). If extraction takes longer than the debate and the debate ends first, extraction is cancelled and this event does not appear. It comes after `debate_started` |
| `compiler_index_warning` | Cleanup failure after extraction (`git worktree remove`, temporary directory removal): `lang`, `sha`, `message` |
| `wiki_build_started` | Start of a code wiki build job (P3, `guardian.py --code-wiki-build`). `project`, `team`, `sha`, `pages: [id]`, `language`, `force`, `host` (as in `debate_started`). In a build job the events of each page have `thread_id` `wiki_<time>_<page id>`, and between them `agent_started`, `retrieval_*`, `citation_check`, `evidence_check` and `agent_finished` appear in the same shape as in a debate (`round` 1, `role` "Domain Expert") |
| `wiki_page_written` | One page was written. `page`, `state` (`fresh`/`stale`/`broken`, computed right after writing; `broken` when unverified citations remain after the follow-up question), `basis_files` (number of front matter `basis` entries), `citations` (number of distinct files the body cites), `unverified` (remaining unverified citations, at most 20) |
| `wiki_page_skipped` | The page was not written. `page`, `reason`: `llm_fallback: …` (built-in fallback answer, no provider answered), `no_answer: …`, `bad_format: …` (no sections), `body_too_large: …`, `scope_missing: …`, `human-written page kept (state: …); use force to overwrite it`, `human-written page kept (it was saved during the build)`, `error: …`. The existing page stays as it is |
| `wiki_build_finished` | `written`, `skipped`, `failed` (number skipped because of LLM or format failures) |
| `evidence_check` | Agent event (P2.5). Only when the final answer has a ```` ```findings ```` block, each time the answer is checked (once more when it is checked again after a follow-up question). `checked` (items checked, at most 50), `mismatched` (number of mismatches), `items: [{path, line, ok, reason}]` (at most 50; `reason` is `verified` or the reason for the mismatch) |
| `reask_failed` | Agent event. A re-ask (citation, ops citation, judge format or action repair) or a retrieval follow-up after it came back from the builtin fallback. `kind` (`citation`, `ops_citation`, `format`, `repair`), `kept` (the answer that was sent back, with its provider, is the turn's answer; false only for a judge answer that lacks its verdict or json, which stays builtin so the round reads UNVERIFIED), `provider` (that answer's provider). Also emitted for a citation or ops citation re-ask whose reply holds no answer (whitespace, a bare `{"requests": ...}` request, also cut off, an engine placeholder), with `kept` true and `reason: "no_answer"`: the first answer and its first citation check stay, no second check runs (since `03d2f9c`); `reason` is absent on the fallback path. Comes before `agent_finished` (since 2026-11-14, `4b79b52`) |
| `agent_finished` | `provider`, `duration_ms`, `steps`, `output_bytes`, `excerpt` (first 600 characters), `fallback` (whether builtin) |
| `verdict` | `approved`, `unverified` |
| `jev_gate` | `gate`, `reasons` |
| `action_proposed` | `id`, `tool`, `status` |
| `action_executed` | Result of a code-change action applied without approval (see "Implementation notes (auto-approval of code changes)" below). `id`, `tool`, `status` (`EXECUTED` `FAILED`, or `PROPOSED` when it could not run because the workspace moved or was locked). Comes after `action_proposed` and before `debate_finished`. Cleanup actions run by an ops project's `auto_run` policy emit the same event |
| `debate_finished` | `status`, `consensus`, `final_round`, `retrieval` |
| `error` | `message` |
| `events_truncated` | – |

`debate_finished.data.retrieval` is the retrieval total of this run only (not an inherited running total like the thread's `context_snapshot.retrieval`). Its shape is `{"results": int, "bytes": int, "by_agent": {"<session key>": {"results": int, "bytes": int, "steps": int}}}` and it counts the repo blocks and MCP calls of all agents. `results` and `bytes` count only successful (`ok: true`) entries. Failed entries and deferred (`deferred`) entries are in neither (the same for by_agent). `steps` is that agent's number of retrieval steps. Without retrieval it is `{"results": 0, "bytes": 0, "by_agent": {}}`. The same `retrieval` object is also at the top level of `--result-file` (result.json), and the hub's `GET /api/debates` list items (and the single-job lookup) read only `retrieval: {"results", "bytes"}` from result.json and attach it (omitted when the file is missing, broken, or from an older job; meta.json is unchanged).

Events never carry full prompts or file contents. Reply excerpts are at the same level as what already stays in the database and the dashboard.

## Node checks in ops debates (added 2026-10-17)

A debate of an ops project (`kind: "ops"`) has no repository. There are no repo blocks, no MCP, no repository map and no code pack. Instead an agent asks for read-only node checks (`cli/llm-agent/ops_checks.py`) with a ```` ```ops ```` block, and the engine runs them and returns the results into the same session. The design and the operating record are in `docs/design/server-ops-project.md`; only the points that touch this document's contract are listed here.

- **Loop**: the same rules as repo blocks. Text after the first block is discarded (`reply_trimmed`, `blocks` 1); when a configured step or turn limit (`GUARDIAN_RETRIEVAL_STEPS`, `GUARDIAN_RETRIEVAL_TURN_BYTES`) is reached, `retrieval_exhausted` is emitted and the agent is told to answer with the results so far; a large step emits `retrieval_warning`. There is no deferral (`deferred`).
- **Events**: `data.via` of `retrieval_requested`/`retrieval_served` is `"ops"`. `results[]` entries are `{op, target, ok, bytes, truncated, error}`. Transcript `retrievals[]` entries are `{step, op, target, ok, bytes, truncated, error, call, host}` (`call` is the check number, counted across the whole debate run).
- **Citations**: agents cite node check results as `[#<call> <check> <host>]`. A citation in the final answer must name a check that returned a result in this run, with the matching check type and host (`OpsChecks.unverified_citations`). If any citation does not match, the engine asks once, listing the checks that exist. Citing a check another agent ran is accepted. The event is `citation_check {cited, unverified, asked, via: "ops"}`, without `mentioned` and `short`.
- **Judge tools**: the judge gets only the tools of its project kind (an ops project: the typed cleanup actions (`ops_actions.py`) and `server_remediation`; a code project: the file and test tools).
- **Prompt versions** (comments on `RETRIEVAL_PROMPT_VERSION` in `debate_engine.py`): p5 the claude system prompt split (section below), p6 judge tools per project kind and target nodes for ops debates, p7 node checks in ```` ```ops ```` blocks and their citations, p8 typed cleanup actions, p9 medium-risk catalog actions, p10 the node-check citation check and its follow-up question. The current value is `2026-10-17.p10`.

## Go API

| Method and path | Content |
|---|---|
| `GET /api/debates/{id}/events?after=N` | Events with `seq > N`. `{"events": [...], "next": <last seq>, "done": <whether the job ended>}`. At most 1000 |
| `GET /api/debates/{id}/events?after=N&follow=1` | NDJSON stream (`application/x-ndjson`). Follows the file and sends new lines, `{"type":"heartbeat"}` every 15 seconds, and `{"type":"end","status":…}` when the job ends, then closes. Maximum connection time = the job's time limit |
| `GET /api/debates/{id}/prompts/{seq}` | The turn prompt of `agent_started` (seq). `{"seq", "bytes", "text"}`. 404 when not saved or a symbolic link. The dashboard timeline's "show prompt" loads it once when expanded |
| `GET /api/threads/{thread}/report` | The thread's result document. Finds the newest of `decisions/projects/*/<thread>.md` and returns `{"thread_id", "project", "markdown", "bytes", "updated_at", "assets": [{name, bytes}]}`. 8MiB cap, symbolic links refused |
| `GET /api/threads/{thread}/report/assets/{name}` | An SVG of the document (`^[a-z0-9][a-z0-9_-]{0,63}\.svg$`). `{"name", "bytes", "text"}` (JSON because of the auth header). 1MiB cap |
| `GET /api/debates` | Adds `events: bool` (events file exists) to the existing list. From P4 on, code wiki build jobs (`kind: "code_wiki"`) appear in the same list (they share the `debate-jobs/` directory). The debate console shows them in the list with a "Code wiki build" chip and leaves them out of the overview's recent debates. The `project` filter applies to build jobs too |

- Authentication is the existing `guard`. The id is checked against `jobIDPattern`. File reads are confined to the job directory.
  (2026-10-18: authentication is the global `withSecurity(withAuth(mux))` chain in `main.go`. `h.guard` only checks that the CLI exists and is not on these read routes.)
- Event lines are passed through as they are, but lines that do not parse as JSON and lines over 1MB are skipped.

## Agents monitor page

An `Agents` menu (monitoring group) is added to the dashboard. Route `agents`, a selected run is `agents?run=<job id>`.

- **Left: run list** — running debates on top, recent debates below. Task, project, status, elapsed time, current round. The list refreshes every 5 seconds.
- **Right: run detail**
  - Header: task, project, thread (links to the Debate result), team, base sha, status, elapsed time, cancel button (the existing `DELETE /api/debates/{id}`).
  - **Agent lanes**: one card each for Expert, critics (several for a panel) and Judge. State (waiting/running/retrieving/done/failed/builtin), current provider, elapsed seconds while running, number of retrieval steps and bytes read, provider fallback warning.
  - **Timeline**: events in time order. Round separators; retrieval requests as op and target chips (`read apps/agent/main.go:100-180`) with their results (bytes, truncation, excluded/error); reply excerpts expandable.
  - While running it uses the `follow=1` stream and reconnects with `after=<last seq>` when cut off. A finished run is read once.
- A "monitor" link is added to the job rows of the Debate console.
- Older jobs without an events file show "this run has no event record" and the existing log.

(2026-10-18: the separate Agents page was later merged into the Debates page (`docs/design/ui-flow-redesign.md`). `#/debates/<job id>` shows a running debate as the monitor (lanes, timeline) and a finished one as its result with an expandable run record. The old `#/agents` address forwards to the Debates page. The view components are in `apps/web-desktop/src/pages/agents/` (`RunList`, `RunDetail`, `LaneCards`, `Timeline`); event interpretation is in `apps/web-desktop/src/lib/agentMonitor.ts`.)

## Stage 2. MCP (optional feature)

- `cli/llm-agent/repo_mcp.py`: a stdio MCP server that exposes `repo_tools` as the tools `repo_list`, `repo_grep`, `repo_read` and `repo_outline` (workspace clone and sha pinned by the `--workspace`/`--sha` arguments; it calls only `RepoTools`). Each tool call is one step. (2026-10-18: `repo_defs`, `repo_refs`, `repo_map`, `repo_callers`, `repo_callees` and `repo_wiki` were added later, so there are 10 tools, and `--allowedTools` lists all 10.)
- Enabled when all three hold (code projects only; ops debates do not use MCP): the optional team member JSON field `"code_tools": "mcp"` (absent or null means repo blocks only; any other value is a validation error when the team is saved), `GUARDIAN_REPO_MCP=1` on the execution host, and the provider that actually runs is claude or codex (codex support is under "codex run" below; other providers always use repo blocks).
  If any of them is missing it silently works with repo blocks, and an MCP member that writes a block still has the block handled (fallback). A member's `code_tools` stays in the team file and in the thread's `team_snapshot` (the key exists only when set).
- Server log: for each turn the engine creates a JSONL log in a private temporary directory (0700) and passes it with `--log`. Each tool call writes one line `{op, target, ok, bytes, truncated, error, paths, spans, mentions, limit[, deferred]}` (no file content; `spans` is the `[start, end]` line ranges the result showed per path, P2.5). The log is the source of truth for results; the engine tails it every 0.25 seconds and emits events.
- Limits: `GUARDIAN_RETRIEVAL_STEPS` (number of tool calls), `_STEP_BYTES` (per call) and `_TURN_BYTES` are applied by the server per turn (the next claude call of the same turn continues counting from the log). `_REQUESTS` (requests per step) does not apply because one call is one request. A read that the step byte limit would cut to less than half is a `deferred` tool error ("request again with a range, symbol or outline"); once the step or turn limit is full, a `budget` tool error. Other large results are cut (`truncated`).
- Events: `retrieval_requested`/`retrieval_served` come in one pair per call with `data.via` `"mcp"` (repo block events have `"via": "block"`). Step numbers continue from the block steps. When a `budget` result appears, `retrieval_exhausted` is emitted once. File-served tracking, transcript `retrievals`, snapshot totals and citation checks use the same path as blocks. For the judge, read calls are re-read at the same sha (applying the log's `limit`) and added to `ContextPack.saw`.
- Prompt: a new session of an MCP member gets the `mcp__repo__*` tool instructions before the repo block instructions, and is told to use blocks when the tools are missing. The rules are the same (cite file:line, do not claim to have read code you did not receive).
- claude run: write `mcp.json` (0600, server name `repo`, `sys.executable repo_mcp.py ...`) into a temporary directory (0700) and remove it afterwards. The turn text of an MCP run is only the text written after the last tool call (narration between tools is excluded).
- If an old claude does not know a flag ("unknown option"), it runs once more without MCP.
- codex run: see "Verified codex flags" below. No temporary file is created (the server definition is entirely `-c` overrides, so there is no config file like `mcp.json`, and `~/.codex/config.toml` is not touched. Authentication and session rollouts use `CODEX_HOME` as usual). If an old codex does not know a flag (`Unknown feature flag`, `unexpected argument`, `unknown configuration field`), it runs once more without MCP. As with claude, the turn text of an MCP run is only the text after the last `repo` tool call.

### Verified claude flags (claude 2.1.289)

```
claude --print --output-format stream-json --verbose --tools "" --effort <level> \
       --mcp-config <mcp.json> --strict-mcp-config \
       --allowedTools mcp__repo__repo_list,mcp__repo__repo_grep,mcp__repo__repo_read,mcp__repo__repo_outline \
       --session-id|--resume <id>        (prompt on stdin)
```

- `--tools ""` turns off only the built-in tools and leaves MCP tools alone. The `tools` of the init event were exactly the four above, with no Read/Bash/Grep and the like.
- `--strict-mcp-config` uses only the servers of `--mcp-config`. `--allowedTools` pre-approves only those four tools.
- Under `--print`, a tool that is not allowed is refused at once without a prompt (run without `--allowedTools`, it is refused with "requested permissions ... haven't granted" and does not hang). So `--permission-mode` is not needed and `bypassPermissions` is not used. The combination `--permission-mode dontAsk --permission-prompts none` also worked but is left out for compatibility with older versions.
- `--allowedTools`/`--mcp-config` take variadic arguments, so another flag must follow them, and the prompt is sent on stdin.

### Verified codex flags (codex-cli 0.160.0)

```
codex exec --skip-git-repo-check --json -o <last.txt> --color never --sandbox read-only \
      --ignore-user-config --strict-config \
      --disable shell_tool --disable unified_exec --disable view_image --disable multi_agent --disable multi_agent_v2 \
      --disable image_generation --disable goals --disable apps --disable plugins --disable browser_use \
      --disable computer_use --disable skill_search --disable tool_suggest --disable hooks \
      -c 'web_search="disabled"' -c 'approval_policy="never"' \
      -c 'mcp_servers.repo.command="<python>"' -c 'mcp_servers.repo.args=["repo_mcp.py","--workspace",...]' \
      -c 'mcp_servers.repo.env={PATH=..., HOME=...}' -c 'mcp_servers.repo.default_tools_approval_mode="approve"' \
      -c 'mcp_servers.repo.startup_timeout_sec=30' -        (prompt on stdin; resume is `exec resume ... -c sandbox_mode="read-only" <id> -`)
```

- **Server registration**: `-c mcp_servers.<id>.*` overrides are enough (`CODEX_HOME` is not changed, so the auth file `auth.json` is used as is and never touched). `--ignore-user-config` skips only `$CODEX_HOME/config.toml`, so the user's other MCP servers (playwright, node_repl and so on) and plugins are not loaded, while auth is still read from `CODEX_HOME`. As a side effect the user config's `model`, `notify` and similar settings do not apply either (only in MCP runs).
- **`default_tools_approval_mode="approve"` is required**. Without it, a read-only exec run refuses every MCP call with "MCP tool call requires approval, but approval policy is never" (an `mcp_tool_call` error in `item.completed`). The value pre-approves only this server's tools; `--dangerously-bypass-*`, `--full-auto` and `workspace-write` are not used (`--sandbox read-only` stays).
- **Built-in tool list (as enumerated by the model)**. A default run has `functions.exec` (a code-mode JS sandbox) and inside it `tools.exec_command`, `tools.write_stdin`, `tools.apply_patch`, `tools.view_image`, `tools.web__run`, `tools.image_gen__imagegen`, `tools.create_goal` and others. With the flags above, `exec_command`, `write_stdin` (= codex's only shell), `view_image`, `web__run`, `imagegen` and the goal tools disappear and only these remain: `functions.exec`/`wait`/`request_user_input` (all chat and code-mode helpers), `clock.sleep`, `collaboration.*` (sub-agents; confirmed to inherit the same settings and have only the same tools), `apply_patch` inside `exec`, `clock__curr_time`, `list_mcp_resources`/`list_mcp_resource_templates`/`read_mcp_resource` (MCP resources; the `repo` server has none), and the four `mcp__repo__repo_*` tools.
- `--disable shell_tool` alone removes the shell (`unified_exec` is the PTY variant of the same tool and is turned off with it). The JS runtime of `functions.exec` has no `require`, `Deno`, `process` or `fetch` (the model checked and reported this itself, `ReferenceError`).
- `--strict-config` and `--disable <unknown feature>` produce startup errors (`Unknown feature flag: ...`, `unknown configuration field ...`). So a codex whose flag names changed fails instead of silently unlocking, and the runner runs again without MCP (repo blocks). A documented key like `tools.view_image` was an unknown field in 0.160.0; `--disable view_image` is right.
- **What cannot be turned off: `apply_patch`**. 0.160.0 has no flag for it (`include_apply_patch_tool` is "ignored", `features.apply_patch_freeform` is removed). Under `--sandbox read-only` writes are refused with "writing is blocked by read-only sandbox". For its check it does read absolute paths outside the repository, but the error text shows no file content (it only repeats the expected lines the model gave: `Failed to find expected lines in <path>:\n<lines the model wrote>`), so all that remains is an oracle in which the model can confirm by guessing whether "exactly these lines are in this file", not a way to read content. A random secret (token) cannot be guessed, so the practical risk is low, but not exactly zero. To narrow it further with a filesystem permission profile (`default_permissions` + `permissions.<n>.filesystem`), a profile that allowed reading only `:minimal` was tried; codex's own sandbox helper then could not execute `/opt/homebrew/bin/codex` and session creation failed (the path differs per platform, not portable, not adopted).
- **Event shape**: calls appear in `--json` as `item.started/completed` `{"type":"mcp_tool_call","server":"repo","tool":"repo_read","arguments":{...},"status":...}`. But when the model calls `tools.mcp__repo__*` from inside `functions.exec`, there is no `mcp_tool_call` item (it really did call that way), so the server log remains the source of truth for tool calls. The engine's `via: "mcp"` event mapping tails the log, so it works unchanged.
- **Post-run check**: if an MCP run's `--json` contains even one `command_execution`, `file_change` or `web_search` item (meaning the lock was broken), that turn is discarded and treated as an error, moving on to the next provider.

### Stage 2 canary check (local)

2026-10-09, this Mac, claude 2.1.289. The workspace clone was a temporary git repository (`hello.py`, `README.md`); the canary was a random string (`CANARY_` + 24 hex digits) in a file in a directory outside the repository. No real secret file was used.

| Attempt | Result |
|---|---|
| Read the canary by absolute path with built-in Read/Bash/Grep | Not in the tool list. The init event's `tools` were only the four `mcp__repo__*`, and the model could not even try |
| `repo_read ../canary_dir/canary.txt`, `repo_list ../canary_dir` | `error: invalid path` (log `ok:false`) |
| `repo_read <canary absolute path>`, `repo_read /etc/hosts` | The leading `/` is stripped and it is read as a repository-relative path, so `error: not found` |
| `repo_read hello.py` | Works, log has `paths:["hello.py"]` |

The canary string appeared nowhere: not in the model output, the tool results, the whole stream-json, or the server log. At first the model refused on its own, so the server check was not exercised; the calls were then requested in a "tool check" context to confirm that the server itself refuses. The same refusal rules are also unit tests in `test_repo_mcp.py` (`../`, absolute paths, secret files, symbolic links, binaries, uncommitted changes). The canary file and directory were deleted after the check.
Before enabling it on the hub, the same attempts are made once more with a canary in the home directory.

### Stage 2 codex canary check (local)

2026-10-12, this Mac, codex-cli 0.160.0, run through the runner as is (`LLMRunner.run_session_prompt(..., repo_mcp=...)`). The workspace clone was a temporary git repository; the canary was a random string (`CANARY_` + 24 hex digits; not a real secret, deleted after the check) in a file in a directory outside the repository.

| Attempt | Result |
|---|---|
| Control: ask `codex exec --sandbox read-only` without the lock to `cat <canary absolute path>` with the shell | **The canary appeared in the output** (a `command_execution` item). So the read-only sandbox does not block reads, and the results below are owed to the lock flags |
| Locked run: have the model enumerate its tools | No `exec_command`, `write_stdin`, `view_image` or `web__run`. The same as "built-in tool list" above |
| Locked run: ask to read the canary by every means (shell, JS `require`/`Deno`/`process`/`fetch`, `apply_patch`, spawning a sub-agent and trying the shell) | No shell (`typeof tools.exec_command === "undefined"`), no file API in JS, sub-agents have the same tools only. `apply_patch` refuses the write or says "Failed to find expected lines" (no content) |
| Locked run: canary absolute path to `repo_read`, `repo_list`, `repo_grep`, `repo_outline` | `error: not found` (read as a repository-relative path with the leading `/` stripped) / for grep, git refuses a path outside the repository |
| `../outside/canary.txt`, `../../outside/canary.txt` | `error: invalid path`; grep says "outside repository" |
| `repo_read hello.py`, then once more in the same session continued with `exec resume` | Works (`resumed: true`, same lock flags). Server log has `ok:true`, `paths:["hello.py"]` |

The server log confirms that the server refused, not the model (every bypass attempt was logged with `ok:false`, such as `read ../outside/canary.txt ok:false invalid path`). The canary string appeared nowhere: not in the model's final answer, the whole `--json`, the server log, or the `~/.codex/sessions` rollout (only the path went into the prompt, never the string). The same refusal rules are also unit tests in `test_repo_mcp.py`. Before enabling it on the hub, the same attempts must be made once more on the hub with a canary in the home directory (the hub's codex version may differ from the local one; in particular, if flag names differ, `--strict-config` makes it run with repo blocks and no MCP).

### Stage 2 canary check (hub)

2026-10-09, site-a-arm1, the service account (ubuntu), HEAD of the workspace clone `~/guardian-workspace`. The canary was `~/canary-outside.txt` (random string, deleted after the check).

- claude run with the same options as production: the init event's tools were only the four `mcp__repo__*` (no built-in tools). The model refused the path bypass requests on its own, so the server check was not exercised.
- So `repo_mcp.py` was asked directly over JSON-RPC: `repo_read ../../canary-outside.txt` and `../.guardian/agent.env`, `repo_list ..`, `repo_outline ../…` gave `invalid path`; absolute paths (`~/canary-outside.txt`, `~/.guardian/llm.env`) gave `not found`; grep with glob `../*` was refused by git as outside the repository. `repo_list /` shows only the repository root (committed files). A grep for `AGENT_TOKEN|CLAUDE_CODE_OAUTH` finds only mentions in committed documents and code.
- The canary string appeared nowhere: not in replies, tool results or the server log.
- Enabling it on the hub: put `GUARDIAN_REPO_MCP=1` into `~/.guardian/agent.env` and restart the agent. `install-agent-service.sh` keeps the `GUARDIAN_REPO_MCP`, `GUARDIAN_RETRIEVAL` and `GUARDIAN_RETRIEVAL_*` lines on reinstall and also writes commented examples of these settings into `agent.env`.

### Implementation notes (differences from the contract)

- Server log records gain `limit` (the byte limit applied to that call) and `deferred`. They are needed to replay the judge's `saw` and to map events.
- Events appear when the log is tailed, so they show up at most 0.25 seconds after a call ends. The tool_use/tool_result of claude's output (stream-json) are not parsed.
- Transcript `retrievals[]` entries do not get `via` (it is only in events).

## Verification

1. `repo_tools` unit tests: exclusion of secrets, symbolic links and binaries; pinned sha (uncommitted changes not included); grep argument injection prevention; size limits and truncation.
2. Retrieval loop unit tests (fake runner): request → result → final answer, budget exhaustion, invalid block, resend on provider switch, event order.
3. Go tests: event polling and streaming, invalid id, skipping broken lines, `end` when the job ends.
4. Web: unit tests of the event → lane state computation, build.
5. Hub integration: rerun the task of `thread_20261008_052043_22a84e` without reference code paths and check that agents read source and cite files and lines, and check the monitor screen.

## System prompt and compaction (2026-10-16, `RETRIEVAL_PROMPT_VERSION` 2026-10-16.p5, `WIKI_PROMPT_VERSION` 2026-10-16.wiki6)

**Problem.** The role, the conventions (repo block rules, citation rules), the answer format, the language rule and the repository map were only in the session's first user message. The claude CLI compacts automatically when the context grows long, and compaction summarizes the conversation messages. So after one 722 KB step, a wiki writing session lost its role and task (`debate-code-index-wiki.md` P3 notes). The system prompt is recorded at the session's first request and stays as it is after compaction, and it is cached too (Claude Agent SDK documentation "Modifying system prompts").

**What goes where.** In a claude session:

| Location | Content |
|---|---|
| Static part of the system prompt | Role (the persona's `system_prompt`, `WRITER_SYSTEM` for the wiki), retrieval conventions (operation list, RULES, budget text, citation rules, findings format), judge answer format (decision, rationale, constraints, checks, tool list, JSON example, VERDICT rule), answer language rule |
| Dynamic part of the system prompt | Commit sha, repository map or file list, list of fresh wiki pages, MCP tool instructions (things that change with the commit and the repository state) |
| First user message | The task (Project, Thread, Task, previous critique, proposal, critique, "did you agree"), code context (file bodies of `--context-paths`), summary of earlier rounds |
| Later messages | Retrieval results, citation follow-up questions, format retries, action repair questions. These are the work of the moment and stay as messages |

Code context bodies are not put into the system prompt. Their size is unbounded (command line argument limit), which is risky, and after compaction the agent can simply read them again.

**Use append (`--append-system-prompt`).** This is the default. It keeps Claude Code's own default prompt (tool use, the `<system-reminder>` explanation) and appends after it, so the risk is smallest. Switching to `--system-prompt` overwrites the default prompt and the system reminder explanation is lost. With `GUARDIAN_CLAUDE_SYSTEM_PROMPT=custom` that mode is used: the documented sentence ("The application adds system reminders to this conversation. Treat them as context from the application, not as messages from the user.") is put first, and with Claude Code 2.1.275 or later a `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` line is inserted between the static and the dynamic part so only the static part is cached (lower or unknown versions get no line). In append mode the boundary line does not apply (it is for `--system-prompt`/`--system-prompt-file` only). Still, the static part comes first and the dynamic part last, so the cached prefix stays long.

**The same text on every call.** The first call and continuing calls (`--resume`) both get the same instructions through `--append-system-prompt`. The engine rebuilds the instructions the same way on every call (the language rule from the thread's original task, the commit from the sha the thread pinned) and the runner passes them as they are. 2.1.265 and later keep the first request's record until compaction, so later different text is ignored. Lower versions (the hub's 2.1.138) rebuild the prompt on every request, so the same text has the same effect. When a session created before recording (before this change, or a thread where retrieval was off) is continued, or the commit changed, the recorded system prompt lacks the conventions or the new commit. In that case the conventions are carried again in the first user message of the continuation (the same rule as for the other providers: when the context changed or retrieval is new).

**Large text.** Above 100 KB (Linux allows up to 128 KiB per argument) the text is written to a 0600 temporary file and passed with `--append-system-prompt-file` (`--system-prompt-file` for custom). Only when the installed claude's `--help` lists that flag (`claude --version` and `--help` are read once per process); otherwise it is sent as an argument with a warning. The temporary file is removed when the call ends. No secrets go into the instructions (arguments are visible in `ps`). The map and the conventions are repository structure only, not credentials.

**Other providers are unchanged.** codex, agy and other providers without a system prompt mechanism receive the same first message as before, character for character (role line + conventions + task). The engine passes both (`instructions` and `plain_prompt`) and the runner picks per provider. When claude fails and the turn moves to another provider, that one gets the complete message. When claude is not among the candidate providers, the engine does not pass `instructions` at all.

**Isolation.** Every claude process the runner starts gets `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`. The operator's `~/.claude/CLAUDE.md` or a CLAUDE.md placed in the working directory (`~/.guardian/llm-sessions`) does not reach the debate agents. Authentication and the rest of the environment are unchanged.

**Operations.** claude below 2.1.265 works too, but rebuilds the prompt on every request, so the cache gain is small. Upgrading the hub's claude (2.1.138) to 2.1.275 or later is recommended (recorded system prompt, boundary line). Every flag in this document was confirmed on 2.1.289.

## Problems found in operation and fixes (2026-10-09)

Problems found in a hub rerun (`dbt_20261009_070408_2aa62c`):

1. **Answers invented without results.** After writing a repo block the model did not stop and kept writing its analysis in the same reply ("I have now read enough code").
   claude `--output-format json`'s `result` holds only the last text block, so the engine never saw the block, and the round 1 Expert and Critic
   gave invented answers with 0 retrieval steps as their final answers. Some replies wrote 6-12KB more after the block.
2. **Several blocks in one reply.** The model imitated several steps at once by writing blocks in a row, so the requests exceeded 8, and the overflow was dropped without saying what was missing.
3. **Exceeding the step budget ended as an error.** Several whole-file reads exceeded 96KB, the rest became `error: budget`, there was no hint to request again, and the judge just answered.
4. **Display.** The overflow notice showed as `? ?`.

Fixes:

| # | Fix |
|---|---|
| 1 | The runner joins **all assistant text blocks** of a turn and passes them to the engine (claude through `stream-json`/the session record; codex and agy by the same rule). Not only the last block. |
| 2 | If a reply has a repo block, the whole reply is treated as a request. **Text after the first block is discarded**, and the result message says "the N characters written after the block were written before seeing the results and were discarded; do not rely on them". Event `reply_trimmed {discarded_chars, blocks}`. |
| 3 | Requests from several blocks are merged, but requests beyond the per-step limit (8) and the step budget are returned as **`deferred`** instead of an error. At the end of the result message the deferred requests are shown as a repo block that can be copied as is, with "request them again in the next step". `budget` only when the turn budget is used up too. |
| 4 | **Budgets are unlimited by default.** `GUARDIAN_RETRIEVAL_STEPS`, `_REQUESTS` (requests per step), `_STEP_BYTES` and `_TURN_BYTES` apply only when set to a positive number; empty or 0 means no limit (for example values for production use: 160KB/640KB). The 64KB cap on reading one file is a file size rule, not a budget, and stays; large files are read by range (`path:START-END`, `path#Symbol`). Only when a limit is set does the prompt state the budget; otherwise it says "no fixed budget; read what you need, use outlines and ranges for large files, and stop requesting when you have enough". Without limits the loop ends only when a reply has no repo block, and the outer bound is the hub's job time limit. Requests that hit a configured limit all come back as `deferred`. |
| 5 | Prompt: "Write one repo block, **last** in the reply, and stop right away. Do not write conclusions before receiving the results, and do not claim to have read code you did not receive." |
| 6 | **Citation check**: the paths the final answer cites are split into **strong citations** and **plain mentions**. A strong citation (`path:N`, `path:N-M`, `path#Symbol`, `line N`/`L N` after a path) passes only if that file itself was provided to this agent in this thread (pack or retrieval result). A plain mention (a path without line or symbol) passes if the file was provided, or if the path string appears **inside content** provided to this agent (for example a string constant `"cli/llm-agent/guardian.py"` in Go code it read). Content means read file bodies, grep lines, outline bodies and the file bodies of the context pack; the repository file list (instructions, pack header), the agent's own answer and `list` results do not count. If the same path is also cited strongly, it counts as a strong citation. If some path fails, the engine asks once ("the following citations are code that was never provided: … request it or drop it from your claims"). Event `citation_check {cited, unverified, asked, mentioned}`. What remains after the question is recorded on the transcript entry as `unverified_citations`. **From P2.5 on**, strong citations are checked per line instead of per file (see "Implementation notes (P2.5)" below). |
| 7 | Display: overflow and deferral show as a "deferred: request again in the next step" warning instead of `? ?`; `reply_trimmed` and `citation_check` show as warnings on the lane card and in the timeline. |
| 8 | **The citation question keeps the output format (A).** The question says "rewrite your **complete final answer** in exactly the format originally required for this turn, and add nothing about this correction (such as saying you removed citations)". For the judge, a format summary built from the same constants as the prompt (`JUDGE_JSON_EXAMPLE`, `JUDGE_VERDICT_RULE`) is appended (json block + final `VERDICT:` line). If the answer after the question lost more required parts (VERDICT line, json when approving) than the original, the original answer is kept and the remaining citations are recorded as `unverified_citations`. |
| 9 | **Judge format guard (B).** If the final answer has no `VERDICT:` line or (when approving) no json block, the engine asks **once** for only the missing parts, without repeating the analysis (independent of step and byte budgets). The received parts are appended to the existing answer; if fewer parts are missing, the joined answer is used, otherwise the original. If it is still incomplete, the existing behavior applies (the fallback rules of `_parse_verdict`). Event `format_retry`. |
| 10 | **Pseudo tool calls (C).** A reply with tool call markup that is not our protocol (`<function_calls>`, `<invoke name=`, `<tool_use>`, `antml:invoke` and so on; quotes inside backticks or code fences excluded) is not a final answer. The markup and everything after it is discarded and the engine asks again, saying "this environment has no such tools and nothing was executed; the repository can be read only with ```repo blocks (or the `repo_*` MCP tools when available), and paths outside the repository cannot be read". It counts as one step against a configured step limit. Without a limit it asks up to 2 times in a row per turn, and the third reply is accepted as its text without the markup, leaving `pseudo_tool_calls` (number of pseudo-call replies in that turn) on the transcript entry. A repo block before the markup is handled normally. Event `pseudo_tool_call`. |
| 11 | **Judge patch rules and action repair (D).** In hub debate `dbt_20261009_150754_bbf882` the judge read only narrow ranges, and its `apply_patch` became `INVALID` at proposal time (it changed `type FleetStreamRequest struct`, which it had not read, and `old` appeared 3 times in the file). The direction of the fix was right; only the patch construction was wrong. (a) The judge prompt (repo block and MCP variants) gets rules from the same constant (`JUDGE_PATCH_RULES`): read the whole enclosing declaration of the area to change, as `path#Symbol` or the full range of the function or type; `old` is the original text exactly as received in this debate and must appear exactly once in the file, otherwise add surrounding lines; do not change lines you have not seen; if unsure, read more before the final answer. Only the sentence on how to read differs per variant (`JUDGE_PATCH_READ_BLOCK`/`_MCP`). (b) **Action repair**: if the judge's final answer approves, then before the actions are recorded the same validation that makes them `INVALID` at recording time (`action_ops.validate_proposal`) runs. If an action is invalid for a repairable reason (`PatchError.repairable`: unseen code, `old` not found, `old` not unique), retrieval is available and the retrieval budget is not used up, the engine asks **once**, regardless of budget, with the exact validation errors ("read the code you need (repo block or MCP tools) and send again a complete final answer with a json block holding all actions and a VERDICT line"; including the format summary and the same patch rules). That reply also goes through the normal retrieval loop (the judge's reads are added to `saw`, MCP reads included), and is validated again at the end. If the new answer lost the VERDICT line or (when approving) the json block, the original answer is kept. Actions still invalid after repair are recorded as `INVALID` as now; if there are only unrepairable errors (bad path, empty `old` for an existing file, unknown tool and so on), the engine does not ask. Event `action_repair`. |
| 12 | **Verdict rationale field.** In a fixing task (`dbt_20261010_142233_*`) all actions were valid, but jev judged both runs `DISPUTED` because "the rationale is weak". jev's `rationale` was the judge answer's prose without the json block, and after an action repair or follow-up question that prose is only a short note like "resending the corrected patch". A `rationale` field is added to the verdict json (the file:line evidence for the problem, alternatives considered and rejected with reasons, the risks accepted and how they are controlled), and this field is passed to jev. Without the field the prose is used as before. |
| 13 | **Answer language rule.** In a mixed-team debate (`dbt_20261011_161828_97b367`) the task and the Critic were in Korean, but the judge's decision came out in Japanese. The prompts of every role (Expert, Critic, Judge) get a language rule: write the whole answer (decision, rationale, constraints, checks and action reasons included) in the task's language, and leave code, paths, identifiers and format markers as they are. In the rerun after the first fix (`dbt_20261011_170120_984763`), the judge first wrote in Korean and then switched to English after the citation question (`Task: === CITATION CHECK ===`), because "the task's language" pointed to the English question text. So the language is detected from the script of the original task (`detect_language`: Hangul → Korean, Kana → Japanese, Han → Chinese, otherwise not detected), the rule names the language directly (`answer_language_rule`), and the same rule is appended to the end of every follow-up message, including retrieval results and the citation, format and action repair questions (`_call`). |
| 14 | **Patch scope and test command flags (2026-10-18).** Evaluation 2 (`docs/analysis/2026-10-13-retrieval-eval2/results.md`) found two judge habits that now matter more because code changes are applied automatically after a passing jev gate. (a) Eight of nine D1 patches added a 2000-line cap the task did not ask for. `JUDGE_PATCH_RULES` now ends with: change only what the task asks for, add no limits, caps, options, renames or refactors it did not request, keep every other behaviour, and put any extra safeguard in an `[Extra]` decision instead of the patch. (b) Three D3 `jev_mcp:run_tests` actions were `INVALID` because they used `go test -C apps/agent`: the tool description named the allowed command prefixes but not their flags. The judge's description now lists the allowed flags of every prefix (`VALUE` marks a flag with a value), says that nothing else is accepted (no `-C`, `cd`, env assignments or shell operators) and that commands run from the workspace root (Go packages as `./apps/agent/...`). A refused flag's error names the allowed flags of that command. `RETRIEVAL_PROMPT_VERSION` 2026-10-18.p12. |

Event contract additions:
- `retrieval_served.results[]` can contain `"deferred": true` entries (`ok:false`, `error:"deferred"`, `bytes:0`). Overflowing requests also become deferred entries, each with its own `op` and `target` (the bundled `op:"?"` entry is gone).
- `reply_trimmed`: agent event, `data {"step": int, "discarded_chars": int, "blocks": int}`.
- `citation_check`: agent event, `data {"cited": int, "unverified": [str], "asked": bool, "mentioned": int, "short": int}`. `mentioned` is the number of plain mentions that passed because the name appears inside provided content (paths that passed because the file itself was provided are not counted). `short` (P2.7) is the number of short-path and line-only citations that were resolved to one file (whether or not they then passed), and they are included in `cited`.
- `format_retry`: judge event, `data {"missing": ["verdict" | "json"]}`. It means the parts missing from the judge's final answer (`verdict`: no VERDICT line, `json`: approving but no decision block) were requested again. At most once per turn.
- `pseudo_tool_call`: agent event, `data {"step": int, "tools": [str], "discarded_chars": int}`. `tools` are the `name`s of `invoke`/`tool_use` tags (deduplicated, empty list if none), `discarded_chars` the number of characters discarded from the markup on. Emitted for every such reply (including the last one that is accepted).
- Transcript entry `pseudo_tool_calls`: int. Present only on turns where a pseudo-call reply was accepted with only the markup removed.
- `action_repair`: judge event, `data {"invalid": [{"index": int, "tool": str, "error": str}], "fixed": int}`. Emitted once, after the repair, on a turn where repair was asked. `invalid` lists every action that was invalid before the question (`index` is the position in the judge json's `actions`, `error` up to 500 characters), `fixed` the number no longer invalid after the repair (0 if the original answer was kept). Absent on turns that did not ask because there were only unrepairable errors.

Implementation notes (where it differs from or adds to the design):
- The claude runner switched to `--output-format stream-json --verbose` (prompt on stdin, `--tools ""` and `--effort` kept). All text blocks of top-level assistant messages are joined with blank lines (thinking and sub-agent messages excluded), and errors are detected from the result event's `is_error` and an empty reply. For codex, the `-o` file holds only the last message, so all `agent_message` items of `--json` are joined; for agy, the `text_delta`s of the `agent_response` steps are collected per step and joined.
- When the per-step request limit (`GUARDIAN_RETRIEVAL_REQUESTS`) is set, requests beyond that number, requests after the step byte budget is full, and reads that fitting the step budget would cut to less than half (except the step's first result, which is cut and served) are `deferred`. Invalid entries (not an object, invalid JSON) are not deferred but answered with an error.
- `retrieval_requested.requests[]` also includes requests that will be deferred. Deferred entries carry `"deferred": true` only in `retrieval_served.results[]`; other entries keep their shape.
- Transcript `retrievals[]` entries get `paths` (the file paths that result showed to the agent), and deferred entries `"deferred": true`. The thread's `context_snapshot` gets `provided_files {session key: [path]}` and `mentioned_paths {session key: [path]}` (repository tree paths whose names appeared inside provided content; up to 400 per session each) for the citation check. A continuing thread inherits both sets as they are. The shape of the `retrieval` total is unchanged.
- `citation_check` is emitted for every final answer with at least one citation. If the first check decided to ask, `asked: true`; the result of the check after the question is also `asked: true` (the remaining `unverified` may change). An answer that was already asked about, or whose retrieval budget is exhausted, is not asked again; the remaining paths are only recorded as `unverified_citations`. Asking does not depend on whether there is a step budget.
- Only full path tokens of readable files in the commit tree count as cited paths (`RepoTools.cited_refs` returns per path whether it is strong or plain). `mentioned_paths` collects tree paths from provided text with the same extraction rule: for block results the engine scans the result text (except `list`), for the context pack the file bodies (`ContextPack.bodies`). For MCP reads the engine has no content, so `repo_mcp.py` logs per call, as `mentions` in the server log, only the names of tree paths that appeared in the result text (up to 400 per call; content is still not in the log), and the engine adds them to the session set. The server does not read the repository again; it only scans the result text it already built, which is simpler than the engine reading the same content again, and a result cut by a limit is scanned only for the part actually shown. A grep result counts the files whose lines appeared as provided. Context pack files, earlier runs' packs, `provided_files` and the same agent's earlier transcript `retrievals` also count as provided.

### Implementation notes (P2.5, evidence)

This implements W3 (file-level citation check), the "L4 verifier" and "enforce a JSON output schema" from the architecture review document. It is a deterministic check without an LLM; `cli/llm-agent/evidence.py` holds the line span and findings helpers. `RETRIEVAL_PROMPT_VERSION` is `2026-10-12.p2.5` (the prompt gained the citation rules and the findings instructions).

**Line-level provenance.** A strong citation (`path:N`, `path:N-M`, `path#Symbol`, `line N`/`L N`/`lines N-M` after a path) passes if one of these holds.

- The file was provided to this agent session **whole** (an untruncated `read`, a whole-file context pack entry).
- The cited line is within ±`GUARDIAN_CITATION_SLACK` lines (default 3) of a line span the session really saw. `N-M` passes when its first line is inside a span and at least half of its lines (not rounded up, `2×covered lines ≥ number of lines`) are inside the seen spans including slack. A single `N` is `N-N`.
- `path#Symbol` overlaps a seen span by at least one line with that symbol's declaration span (`code_select.symbol_spans`, leading comments included). If the language has no parser or the symbol is not found, it passes if the file has any seen span at all (the same as before P2.5).
- A plain mention (no line or symbol) keeps the earlier rule (the file was provided, or the path string is inside provided content).

Seen lines per result (computed from the result text, so MCP and repo blocks are the same): an untruncated whole `read` = the whole file (`[1, 1000000000]`). A truncated `read` = the numbered lines that arrived complete (the last line may be cut in the middle and is left out). A `path:A-B`/`path#Sym` read = the numbered lines (import block included) and the outline entries (one line each). `outline` = the entry lines. `grep`/`refs` = the matching lines. `defs`/`map`/`list` = none. Context pack = the whole file, or the selected lines plus the import block and the outline entries (`ContextPack.spans`; `visible`/`whole`/`saw` of the judge patch validation are unchanged). Since outline entries are seen, that declaration line and `path#that_function` pass.

The follow-up question for unverified citations distinguishes two kinds: "code never received" (the file itself was never provided: list of paths) and "only part of the file received" (`path:20 (you were shown lines 5-6)`, at most 10 seen spans). Both can come in one message, and the question is asked once per turn. An item of `citation_check.unverified` is the path for a file never received, and the mismatching citation string (`path:20`, `path:5-30`, `path#Sym`) for a partly received file (never-received paths first). The same list also goes into the transcript's `unverified_citations`.

**Persistence.** The thread's `context_snapshot.provided_spans`: `{session key: {path: [[start, end], ...]}}`. Merged and joined spans, up to 400 paths per session and 50 spans per path. A path beyond the cap is not put into `provided_spans` and stays only in the file-level `provided_files` (counted as if the whole file was provided, as before P2.5). Threads without `provided_spans` (before P2.5), files from earlier runs' transcripts and files of earlier context packs keep the file-level rule. Context pack spans are recomputed from the pack on every run and are not stored. A continuing run inherits the stored spans. `provided_spans` exists only once the session has seen code.

**findings block (optional).** At the end of its final answer an agent may write a ```` ```findings ```` block (JSON `[{"claim", "path", "line", "quote"}]`, at most 50 items, quotes up to 300 characters). The judge's `VERDICT` line must be last, so the block goes before it. The closing fence must be at the start of a line (JSON strings cannot hold newlines, so a ```` ``` ```` inside a quote cannot end the block), and only the last block is used. The engine checks each item against the file at the pinned sha: the path must be in `readable_paths`, and the `quote` (with the result's `N| ` prefix removed and whitespace normalized) must be a substring within `line`±slack lines (a multi-line quote is looked for that many lines further down). A path that is not a readable file, a line past the end of the file, or a missing quote is a mismatch. Mismatches are merged into the citation question ("These findings do not match ...", one merged question per turn). The block is not removed from the stored answer. If the items are invalid JSON or not a list, it is "block present, 0 items checked". `_parse_verdict`/`judge_missing_parts`/`parse_judge_block` read past the `findings` block (so a `VERDICT: APPROVED` or ```` ```json ```` inside a quote never mixes into the verdict), and the judge's json block and `VERDICT` line format are unchanged.

**Events and records.**
- `evidence_check` (table above). Emitted every time an answer with a block is checked.
- Transcript entry `findings: {"checked", "mismatched"}`: only on final answers with a block (not stored in SQLite; only in the run result and the thread JSON, like `retrievals`).
- `retrieval.findings` of the run result (and of `debate_finished.data.retrieval.findings`): `{"<provider>": {"answers", "with_findings", "checked", "mismatched"}}`. `answers` is the number of final answers that provider gave (built-in fallback answers and runs with retrieval off do not count), `with_findings` the answers that had a block, `checked`/`mismatched` totals over final answers. The key is always present and may be empty (`{}`). Provider names are the same as the transcript's `provider` (`claude-cli` and so on).
- MCP server log lines gain `spans`. A log line without `spans` (older server) counts that file as provided at file level only.
- The dashboard was not changed. Unknown event types are skipped (as confirmed for P2.1).

**Tests.** `test_evidence.py` (span rules, seen lines per result, a same-name `Handle` decoy, slack, ranges and symbols, persistence and pre-P2.5 threads, caps, MCP log, findings check, events and merged question, judge format, prompt).


**Short citations (P2.7).** On the hub, pages where the wiki writer cited briefly, like `guardian.py:755` or `(line 510)`, were saved with `citations: 0`, and nothing was verified. Debate answers had the same gap. The rules (debates and the wiki writer use the same check, `evidence.scan_short_refs`, `DebateEngine._resolve_short`):
- Short path: a token that ends in a file name whose extension starts with a letter and is a suffix of a tree path but not itself a tree path (`guardian.py`, `llm-agent/guardian.py`), followed by a strong suffix (`:N`, `:N-M`, `#Sym`, ` line N`), is a citation. Tokens not in the tree (`example.com:443`) and `go1.26.8`, `12:30`, `http://host:9870/`, `Python 3.13` do not count. A bare name without a suffix is not a citation, but it becomes the "nearest file" of a line-only citation below.
- Resolving the file: if exactly one of the candidates is a file this session saw (provided, seen spans, file level), it is checked as a strong citation of that path with the P2.5 line rules. If none was seen and there is one tree candidate, "never shown" (unverified). If two or more were seen, or none was seen and there are two or more tree candidates, "ambiguous short path" (unverified, up to 5 candidates).
- Line references without a path: `line N`, `lines N-M`, `(line N)`, `(L N)`, `줄 N`. If an earlier path (full or short, even a bare name) is on the same line, that file; otherwise the nearest earlier path in the same list item or paragraph; `line N of <path>` takes the path after it. Nested list items inherit their parent item's file, and sibling items do not mix. Blank lines and headings clear the context. Without a file it is unverified, "line reference without a file". `path line N` attached to a path counts only as a path citation.
- Fenced code blocks: they inherit the file of the paragraph right before the blank line, and blank lines inside the block do not clear the context. If the info string is missing or text-like (`text`, `tree`, `console`, `output`, `log`, `ascii`, `diagram`), call tree comments like `(line 510)` are checked like a paragraph. Blocks with a language like `python` or `go` are program text, so only explicit paths count there (to avoid false positives like `print("line 5")`). ```` ```findings ```` blocks are skipped.
- `unverified` items are human-readable strings: `guardian.py:755 (never shown: cli/llm-agent/guardian.py)`, `dup.py:12 (ambiguous short path: a/dup.py, b/dup.py)`, `line 12 (line reference without a file)`. A citation resolved to a full path whose lines then mismatch is `path:N` as before. The follow-up question asks for full repository paths (`cli/llm-agent/guardian.py:755`) and lists candidates for each unresolved short citation.
- Prompt: the citation instruction gained "full repository path", and `RETRIEVAL_PROMPT_VERSION` is `2026-10-13.p4` (tests updated on purpose: `test_code_wiki.GOLDEN_INTRO_HASHES` (3 byte hashes of the intro) and its version assertion, the version assertions of `test_evidence.PromptTests` and `test_go_facts`, and `short: 0` in `test_evidence`, `test_retrieval_loop` and `test_repo_mcp_engine`, which pin the `citation_check` shape). The wiki writer is `WIKI_PROMPT_VERSION = 2026-10-13.wiki4` and has a rule against citing by name only, so `--stale` picks the existing pages again.
- Limits: the staleness check (`code_wiki.broken_citations`) still looks only at full path citations. A short citation is verified once at writing time and not checked again after the page is saved. The wiki's `citations` count includes the files of resolved short citations.
- Tests: `test_short_citations.py` (scanner rules, verified and unverified hub examples, ambiguous names, never-shown files, line references without a file, false positive prevention, wiki writer saving).

### Implementation notes (P3, code wiki)

The full design and implementation are in "Implementation notes (P3)" of `docs/design/debate-code-index-wiki.md`; only the points that touch this document's contract (events, records, citation check) are listed here.

- **The new op `wiki`** (`page` or `query`) appears in `retrieval_requested.requests[]` and `retrieval_served.results[]` with the same entry shape as other ops (`{op: "wiki", target: <page id or query>, ok, bytes, truncated, error}`). The MCP tool is `repo_wiki`, and `paths`, `spans` and `mentions` of its server log lines are always empty.
- **Citation check**: wiki results provide no file, no line span and no plain mention (`provided_paths`/`provided_spans`/`mentioned_in`). A `path:N` written from the wiki alone is questioned as code never received. Transcript `retrievals[].paths` is `[]` for wiki entries.
- **Events**: `debate_started.data.context` gained `wiki_pages` and `wiki_stale`. A wiki build job emits `wiki_build_started`, `wiki_page_written`, `wiki_page_skipped` and `wiki_build_finished` (table above). It is not a debate, so there is no `debate_started`/`debate_finished`.
- **Thread `context_snapshot`**: only when the project has wiki pages, `wiki: {"shown": [page ids listed in the first prompt], "states": {id: "fresh"|"stale"|"broken"}}` (no key without pages or with `GUARDIAN_CODE_WIKI=0`).
- **Prompt**: `RETRIEVAL_PROMPT_VERSION` is `2026-10-12.p3`. The wiki section (`=== Code wiki (navigation notes) ===`) is appended after the map only when there are `fresh` pages; without them the prompt is byte-identical to p2.6.

## Result document (2026-10-13)

When a debate is saved, `cli/llm-agent/debate_report.py` writes `decisions/projects/<project>/<thread>.md` in report form (`latest_debate_transcript.md` has the same content). Sections (the headings are written in Korean): a metadata table, `요약` (summary: decision status, verdict, checks, proposed actions, jev gate), `진행 흐름` (flow: a deterministic Mermaid `sequenceDiagram`), `라운드 요약` (round summary, `summary.svg`), `검색 사용량` (retrieval usage, `retrieval.svg`, omitted without retrieval), `토론 기록` (debate record, the raw transcript).

- SVGs live in `<thread>.assets/` and the md references them with relative links `![alt](<thread>.assets/<name>.svg)`. No scripts, no `foreignObject`, no external href, white background. SVGs no longer generated are removed at the next save (symbolic links are not touched).
- `JUDGE_DIAGRAM_HINT` in the Judge prompt: if it helps explain the proposal, it may include at most one ```mermaid diagram (optional). Verdict parsing is not affected.
- Dashboard: the "result document" button of a finished debate → rendered with marked + DOMPurify; SVGs are fetched through the asset API, sanitized and inlined; Mermaid is lazy-loaded only when a block exists (`securityLevel: 'strict'`), and shown as code when rendering fails. "Download MD" saves a single .md with the SVGs embedded as data URIs (Tauri: the `save_download` command into `~/Downloads`, never overwriting).

### Requirement ids and the trace keys of `final_decision`

When the Judge approves, `final_decision` (result.json) gets the following keys besides `decision` and `checks`. Without approval they are absent.

- `rationale`: string. Paragraphs and line breaks are kept, cut at 4000 characters (the value sent to jev is one line, 2000 characters, as before).
- `constraints`: list of strings (the same normalization as the existing `parse_constraints`). `constraint_refs`: a list of lists of the same length as `constraints`; `[]` for items without refs. `check_refs` works the same way for `checks`.
- Each item of `actions[]` (the action snapshot) gets `refs` (list of requirement ids, `[]` if none) and `file_path` (`arguments.file_path`, or for `apply_patch` the file when all edits target the same file; the key is omitted otherwise).
- Only `^R\d{1,2}$` is accepted as a requirement id. Other forms are dropped; duplicates are removed while keeping the order.
- In the decision database it is stored as `decisions.decision_trace` (JSON: `rationale`, `constraints`, `constraint_refs`, `check_refs`) and comes back with the same keys in `get_thread_history()["decision"]`. Action `refs` are only in the result.json snapshot.

Judge input conventions (the older string-only form stays valid):

- Each required change in `decision` starts its paragraph with `[R1] short title: content`, `[R2] ...`. Additional decisions start with `[추가]`/`[Extra]` and have no id.
- `constraints` and `checks` items are strings or `{"text": "...", "refs": ["R1", "R3"]}`. Each action may have `"refs": ["R2"]`.
- Action `refs` are removed at parse time (`parse_actions`), so they never reach tool argument validation, the stored tool input or the jev request. `parse_action_refs` returns them separately, in the same order as `parse_actions`.
- The prompts are `JUDGE_REQUIREMENT_RULE` (debate_engine.py) and `JUDGE_JSON_EXAMPLE`. The expert and critic prompts and the retrieval instructions (`RETRIEVAL_PROMPT_VERSION`) did not change.
- The result document's `요약` (summary) gained `근거` (rationale), `제약` (constraints, with refs appended as `(R1, R3)`), `확인 항목` (checks, the same way), a `요구사항` (requirement) column in the action table, and a `추적` (trace) table keyed by the `[R<n>]` marks of the verdict paragraphs (number of constraints, number of checks, files, "변경 없음" (no change) when there is no action).

### Implementation notes (auto-approval of code changes)

The approval tab waited for a person's decision on every file change and blocked automation (2026-10-16), so code changes are applied without approval. Code: `action_ops.auto_run_actions`, the decision `is_auto_approvable`, the switch `auto_apply_enabled`.

- **Scope**: only `jev_mcp:apply_patch` and `jev_mcp:execute_code_action`. `run_tests` and `server_remediation` execute code, so a person still decides them in the approval tab. An `execute_code_action` with `run_test` on runs tests after the write, so it is also left to a person.
- **When**: (1) right after the debate ends with the jev gate PASSED and the actions are stored as `PROPOSED` (`DebateEngine._auto_apply`), (2) right after `--jev-recheck` passes and releases HELD (`recheck_thread` returns `auto_applied`). HELD and INVALID are not touched.
- **Execution**: the existing `run_action` as is (revalidation, refusal of a stale base commit, workspace lock, protection against the `PROPOSED -> RUNNING` race). `decided_by` is `auto:engine`, which tells it apart from runs by a person. The actions of one thread run in ascending id order so the `sibling_hashes` chain is right, and if one does not end `EXECUTED`, it stops there and the rest stay `PROPOSED`.
- **How to turn it off**: `GUARDIAN_AUTO_APPLY=off`, or the hub's `GUARDIAN_UI_ACTIONS=off` (the engine does not do what the dashboard cannot). On by default.
- **What stays**: changes are written only into the workspace clone and not pushed. Publish, which puts them into the repository, is still a person's last gate. With `GUARDIAN_JEV_GATE=off` the gate is always PASSED, so files change on the judge's approval alone.
- **Tests**: existing tests that check the engine flow as `PROPOSED -> approval -> EXECUTED` turn it off with `testenv.set_auto_apply(self, "off")`. Auto-apply is tested by `test_auto_apply.py` and `test_patch_ops.EndToEndTests`.
- (2026-10-18: `GUARDIAN_AUTO_APPLY` accepts `off`, `0`, `false` and `no` as off. In an ops project the same `auto_run_actions` runs the low-risk catalog cleanup actions listed in the project policy's `auto_run`, up to `max_auto_runs_per_day` a day, with `decided_by` `auto:<project>`. This path needs a real jev PASSED, so with `GUARDIAN_JEV_GATE=off` ops actions are not run automatically. Details in `docs/design/server-ops-project.md`.)
- (2026-10-30, prompt `p18` (the GitHub mirror's number; `p21` after the merge with the local history): the judge prompt used to say "Actions are NOT executed automatically: a person reviews and approves each one" while the engine applied the file changes itself. `judge_actions_note` now says which actions run without a person: in a code project the file changes above, once the decision passes jev (with the jev gate off, once the judge approves), and in an ops project the `auto_run` actions the judge holds, with the daily cap. With `GUARDIAN_AUTO_APPLY=off`, in design projects and in ops projects without `auto_run` the old sentence stays. In the same version a plan judge in a project with `policy.batch.auto` is told that the batch gate may start a plan (`judge_plan_rules`), and ops and design experts and critics get their own turn text (`EXPERT_ASK`, `CRITIC_ASK`) instead of "architectural proposal" and "security flaws, and performance risks".)

### Implementation notes (judge reasons and turn texts, prompt `p20`, the GitHub mirror's number; `p22` after the merge with the local history, 2026-10-30)

- **Rejections reach the expert.** The next round's expert used to get only the critics' text, so it answered points the
  judge had already set aside and never saw what the judge wanted. `DebateEngine._judge_reasons` hands the last
  rejecting judge answer (without the verdict line and the findings block, at most 12,000 characters) to the next
  round's expert, also when a thread is continued. The critics do not get it: they judge each proposal on their own
  (operator's decision). The judge is asked, when it rejects, to list what must change and to leave out points it did
  not accept (`JUDGE_REJECTION_RULE`).
- **Turn texts.** A code critic stays within the focus of its persona; "edge cases, security flaws, and performance
  risks" is the default for a persona that names none (the `guardian-dev` QA and release critics were sent after
  security and performance). The mermaid hint is for code judges only. A first-round expert no longer gets an empty
  "Previous Critique" line. The citation re-ask of an MCP session names the repo tools next to the repo block.
- **Merged into the flow steps (`p22`).** The local engine runs rounds through flow steps, so the reasons travel as
  `FlowRun.reasons_in` and `_carried_in` writes them, after the critique, into the lead's draft, consolidate and
  rewrite turns and into every pipeline stage, wherever the critique already went. Panel members and versus sides do not get them, as they get no
  critique either (a panel round starts fresh, versus is one round). The round loop recomputes them after each judge
  turn, so a later run of the same round count sees the newest rejection.

### Implementation notes (jev fallback constraints and the CLI chat, 2026-10-30)

- **Fallback constraints.** When the judge writes no constraints, `jev_gate.critique_points` turns the critique's
  bullets into constraints for jev. That made every point a constraint, also advice and points the judge set aside, so
  jev could dispute a decision over a `[권고]`. A critique that grades its objections now gives only its `[차단]` lines;
  with none, the single fallback constraint is used. Ungraded critiques (the code teams) keep their bullets.
- **Neutral wording.** The fallback constraint reads "The decision answers the critics' blocking concerns it accepted,
  and the evidence it cites supports it." (was: the red-team's security, edge-case and performance concerns), and the
  context jev reads labels the critique "Critique:" instead of "Red-team critique:". jev has no version constant; the
  debate prompts are unchanged.
- **CLI chat.** `run_supervisor_chat` sends the persona's `system_prompt` as the role line (`run_prompt(system=...)`),
  without the default "Linux System & SSH Security Engineer" header and without the persona JSON, so `temperature` and
  the focus lists no longer reach a model on any path (`debate-teams.md`).

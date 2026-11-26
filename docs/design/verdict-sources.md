# Verdict sources: pluggable judges in the close (design)

English | [한국어](verdict-sources.ko.md)

Status: proposed 2026-11-01. The direction (a layer of verdict sources in the close, the LLM writer kept, sources
registered on the hub and only chosen in flows) was agreed with the operator the same day; the details below await
review. S1 (registry and shadow run) built and deployed 2026-11-02 as `c4a1ef6`; S2-S5 not built.
[jev-verdict.md](jev-verdict.md) is the first source of this layer.

## Problem

The close of a flow ([flow-graph.md](flow-graph.md)) has one judge: an LLM turn that gives the verdict and writes the
decision, followed by the fixed jev gate. Two questions came up:

- Can the verdict come from something that is not an LLM? jev (TypeSafe) judges but does not write
  ([jev-verdict.md](jev-verdict.md)); other services and checks (a security scanner behind an MCP server, the project's
  tests) could judge too.
- How would a flow use such a service without letting the flow file, which anyone with dashboard access can edit, run
  arbitrary commands or reach arbitrary servers?

Plugging these in as LLM providers does not work: a provider must write the judge's whole answer (verdict, decision,
rationale, constraints, actions), a provider cannot say "unsure", and the provider slot exists on every seat.

## Idea

```
close = [verdict source 1, source 2, ...]  (order and role per source)
      + writer (the close's character, an LLM)
      + shared checks (action validation, the jev gate)
```

A **verdict source** answers one question about the round, from the same material the LLM judge reads, and does
nothing else. The LLM keeps writing the decision.

### The contract of a source

- **Input:** the task, the judge material (proposal or drafts, critique, the summary if any), the project id and kind,
  the commit and the workspace path for sources that read code.
- **Output:** `approve`, `reject` or `uncertain`, with an optional probability, reasons (short texts) and metadata
  (model, latency, cost).
- **Rules:** read-only; a time limit; a failure or timeout counts as `uncertain` and is recorded; a source receives only
  its own keys (the `env_policy` rule: `TYPESAFE_*` and the like never reach an agent).

### Roles

| Role | Meaning | Example |
|---|---|---|
| `decide` | When sure, the source decides the round; when unsure, the next `decide` source is asked; when none is sure, the LLM judge decides as today | jev verdict (jev-verdict.md V3) |
| `alarm` | Never changes the verdict; when it fires on an approved round, automatic application stops and a person decides | jev "fundamental flaw" score |
| `gate` | An approved round passes only if the source approves; otherwise the round is not approved and its reasons go to the next round | project tests, a security scan; the jev gate is this role on the written decision |

`alarm` and `gate` sources run after an approval; `decide` sources run before the writer. On a `decide` approval the
writer may still object (jev-verdict.md V4), which leaves the round unapproved.

## Kinds of source

| Kind | What it does | Built on |
|---|---|---|
| `llm` | Today's LLM judge (the default and the last `decide` step) | the engine as it is |
| `jev` | One System One request with the verdict questions | `jev_verdict.py` (J1) |
| `mcp` | Calls one tool of an MCP server registered on the hub and reads its answer | `mcp_stdio.py`, the servers of `configs/mcp_servers.json` (the jev gate uses one) |
| `command` | Runs an allowed command (a project's test command) in a scratch copy of the workspace; exit 0 approves | the project's `test_commands` and the run policy of `jev_mcp:run_tests` |
| `http` (later) | Calls an external verdict service | – |

## Registering a source (operator, on the hub)

A flow names a source by id; what the source runs is defined by the operator, the way command sets are:

- **Servers.** An MCP server is added to `configs/mcp_servers.json` (reviewed and deployed with the code), its keys only
  in its own environment (llm.env), never in a flow.
- **Source files.** `configs/verdicts/*.json` (builtin) and `<GUARDIAN_DATA_DIR>/verdicts/*.json` (the operator's),
  validated on save, with a `reviewed` date (180 days, as for command sets):

```json
{
  "id": "security-scan", "name": "Security scan", "kind": "mcp", "reviewed": "2026-11-01",
  "server": "secscan", "tool": "scan_change",
  "arguments": {"diff": "${proposal}", "repo": "${workspace}"},
  "answer": {"probability": "result.risk_free", "approve_at": 0.8, "reject_at": 0.2, "reasons": "result.findings"},
  "timeout_s": 60, "projects": ["code"], "roles": ["gate", "alarm"]
}
```

- **Arguments** use fixed placeholders only: `${task}`, `${proposal}`, `${critique}`, `${material}`, `${workspace}`,
  `${commit}`, `${project}`. No shell, no free expressions.
- **Answers** are read by a fixed form: a dotted path to a probability with two thresholds, or a path to a boolean, plus
  an optional path to reasons. No code in source files.
- **Read-only.** The tool must be read-only; the file states it and the validator refuses tools the hub marks as
  writing. Changes still go only through actions and their approval.
- **Roles allowed.** A source lists the roles it may take (an untested scanner as `alarm` only, for example).
- `jev` and `command` sources have their own small forms (the jev question set and thresholds; the project test
  command name).

## Using sources in a flow

The close gains `verdict_sources`, an ordered list of `{source, role}`:

```json
"close": {"character": "jev_mcp_decision", "providers": ["claude"],
          "verdict_sources": [{"source": "jev", "role": "decide"},
                              {"source": "project-tests", "role": "gate"},
                              {"source": "jev-fundamental", "role": "alarm"}]}
```

Empty or absent means today's close. Validation refuses an unknown source, a role the source does not allow, a source
whose projects exclude the flow's, and any source still in shadow (see "Shadow first").

### Editor

The close panel lists the hub's sources by name with their kind, roles and shadow agreement; the operator adds one,
picks its role and orders the list. For the operator, choosing jev therefore looks like picking a provider, while the
flow stores a source with a role. The close node on the canvas shows the sources in order with their role, and the
read view colours each by its result.

## Shadow first

Every new source starts in shadow: it runs next to today's LLM judge, records `verdict_source` events (`source`,
`role: shadow`, result, probability, reasons, latency, error) and changes nothing. Its agreement with the LLM judge, and
for `gate` and `alarm` its hits and false alarms, are read with a report (the J1 report generalised). The operator
lifts the shadow per source when its numbers meet the bar written in its file. jev is the first source in shadow
(J1, running since 2026-11-01).

## Events and the result

- One `verdict_source` event per source per round (role, result, timing), plus the existing `verdict` event of
  today's shape (the event contract of jev-verdict.md: screens keep reading it).
- `final_decision.verdict_by` names the source that decided (`llm`, or a source id), and `final_decision.sources` lists
  every source's result, so the result view and the report need no events.
- An `alarm` that fires holds automatic application like a `DISPUTED` jev gate and says which source fired.

## Relation to what exists

| Today | In this design |
|---|---|
| The LLM judge | the `llm` source, last of the `decide` steps |
| jev verdict, J1 shadow | the `jev` source in shadow; J3 of jev-verdict.md is giving it the `decide` role |
| The jev gate (`jev_submit_decision` on the written decision) | stays as it is; shown in the same chain as a `gate` on the decision text. Moving it into this layer is optional and later |
| Judge tools (`jev_mcp:run_tests`, patches) | unchanged: they are actions the writer proposes, not verdicts |
| Repository MCP for the judge (`code_tools: "mcp"`) | unchanged: it lets the LLM read code; a source may read the workspace through its own kind |

## Phases

| Phase | Content | Done when |
|---|---|---|
| S1 | Source files, registry, validation, `--verdict-sources` report and API; `jev` as the first registered source; shadow events for every registered source | the J1 shadow runs as the `jev` source; tests with fake sources |
| S2 | Roles in the engine: `decide` chain with the LLM fallback, `gate`, `alarm`; `verdict_by`, `sources`; the writer turn and objection (jev-verdict.md J3) | golden dump unchanged without sources; seeded tests per role |
| S3 | `mcp` kind (argument placeholders, answer paths, read-only check) | a fake MCP server in tests; one real read-only server in a sandbox |
| S4 | `command` kind (project test command in a scratch copy) | the run policy of `run_tests` reused; tests |
| S5 | Dashboard: the close panel list, the canvas, the read view, the verdict chain, the monitor (the UI impact table of jev-verdict.md) | dashboard tests; a finished run with each role checked in a browser |

J2 of jev-verdict.md (the measurement) goes on during S1; the jev source gets a role only after it.

## Non-goals

- Sources that write the decision or change anything.
- Arbitrary commands, servers or code named in a flow file.
- Removing the LLM writer or the jev gate.

## Open points (for review)

- Whether the jev gate moves into this layer later or stays beside it.
- Cost display per source (calls, seconds; money where the service reports it).
- Whether a `gate` failure should end the thread at once instead of carrying its reasons to the next round.
- The bar for lifting a source's shadow: one rule for all sources, or per role.

## Implementation notes

### S1 (2026-11-02, deployed as `c4a1ef6`)

- Deployed 2026-11-02; the hub's `--verdict-sources` lists `jev` in shadow (switch from the existing
  `GUARDIAN_JEV_VERDICT=shadow`), `GET /api/verdict-sources` answers 200, and the J2 count holds its first record (a
  `jev_verdict` event of that morning's debate, agreeing).

- `cli/llm-agent/verdict_sources.py`: validation, the two stores (`configs/verdicts/`, `<GUARDIAN_DATA_DIR>/verdicts/`;
  builtin override `GUARDIAN_VERDICT_SOURCES_DIR`), `JevSource`, `run_shadow`, `collect`/`summarize`/`report`.
  `configs/verdicts/jev.json` is the only source: roles `decide`, `alarm`; all three project kinds; question set 2;
  0.8 / 0.2; bar 30 verdicts and 0.9 agreement (the J2 bar of jev-verdict.md).
- File keys beyond the design: `timeout_s` (1-600, default 60) and `shadow_bar` (`min_verdicts`, `min_agreement`).
  S1 only reports whether a bar is met; lifting a shadow is S2. The open point on one bar for all sources or per role
  is answered for now by a bar per source file.
- `mcp` and `command` files are refused ("not built yet (phase S3/S4)") instead of being stored unrunnable.
- Switch: `GUARDIAN_VERDICT_SOURCES=shadow`; the J1 switch `GUARDIAN_JEV_VERDICT=shadow`, which the hub has, turns the
  same shadow run on, so the deploy needs no change to `agent.env`. `GUARDIAN_JEV_VERDICT_YES` / `_NO` still override
  the jev file's thresholds.
- Events: `verdict_source` per source per round (`source`, `kind`, `role: "shadow"`, `verdict`, `probability`,
  `reasons`, `latency_ms`, `error`, `llm`, `agree`, plus the kind's fields; seat `close`). The jev source also writes
  the J1 `jev_verdict` event unchanged, and `verdict_disagreement` gained `source` (jev keeps its `jev` and `p_approve`
  fields). The report counts a job's `jev_verdict` only when the job has no `verdict_source` for jev, so the J2 count
  goes on without a break or a double count. A source that runs past `timeout_s` is recorded as uncertain; jev's
  request timeout is `timeout_s`, there is no separate watchdog thread.
- The engine passes `{project, kind, commit, workspace}` as the source context; jev ignores it.
- `guardian.py --verdict-sources` (exit 2 on an invalid file), read-only `GET /api/verdict-sources`, a `scripts/ci.sh
  config` check (stale `reviewed` is a warning). `--jev-verdict-report` is unchanged.
- No flow field yet: `close.verdict_sources` comes with S2, since every source is in shadow and validation would refuse
  it anyway. No prompt change; the golden dumps are unchanged.
- Tests: `test_verdict_sources.py` (validation, fake sources, an engine run with an injected source, the report over
  both event shapes); `test_jev_verdict.py` runs unchanged apart from the fake's `limits` argument.

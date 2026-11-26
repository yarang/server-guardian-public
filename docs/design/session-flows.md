# Flows from operator sessions (design)

English | [한국어](session-flows.ko.md)

Status: proposed 2026-11-25. Decided by the operator the same day: SD1 drafts then promotion, SD2 runs allowed,
SD3 kind and seats only; SD4-SD6 decided 2026-11-26 as recommended (section 7). SF1 built 2026-11-26, not
deployed; SF2-SF4 not built.
Origin: "Can the MCP be reworked so it can make workflows?" An operator session (Claude Code, agy) on a workstation
already reaches the hub through the session link (`docs/design/operator-sessions.md`) and can start a quick review
(`review_submit`, V2d of `docs/design/verification-layer.md`). It cannot list, draft or start a flow
(`docs/design/flow-graph.md`): the only way today is the dashboard's flow editor and start form.

## 1. Goal and non-goals

Goal: from an operator session, without the dashboard,

- see which flows exist and what a flow may contain (jobs, kinds, characters, limits);
- draft a flow in a short form (a kind and the characters in its seats), have the hub check it, fix it, check again;
- keep the draft on the hub, where a person promotes it to an operator flow in the dashboard;
- start a run of an existing flow (stored, template or team) with seat swaps and parameters, and read its result.

Non-goals:

- No full flow graph from a session (SD3): no free seats, bundles, links, input modes or close settings. The canvas
  editor stays the only place to draw those.
- No direct write to the operator flow store, and no overwrite of a builtin or operator flow (SD1).
- No run of a draft. A draft runs only after a person promoted it.
- No new authority: a session-started run proposes actions like any run and acts on nothing without a person (SD4).
- No thread continuation from a session: a run starts a new thread, as `POST /api/debates` already requires for a
  flow start.
- No resident agents: mail grants nothing, and resident agents get no flow tools.

## 2. Constraints

1. The workstation is untrusted (`operator-sessions.md`, Architecture rule 2). Every frame is re-validated on the hub,
   unknown fields are refused, and `started_by` is the link's own workstation, never a client value.
2. The session link spawns nothing. Like the review ops, the link entry calls the hub's own HTTP API on loopback with
   the hub's `AGENT_TOKEN`, which is read on the hub and never crosses the stream.
3. Every layer of the session chain gets each new op: the tool in `session_mcp.py`, the daemon's `MCP_OPS`
   (`session_local.py`), the link client's `OPS` (`session_uplink.py`) and a validator plus handler in
   `session_link.py`. Running MCP processes keep their old code until the CLI session restarts.
4. A flow never widens tools. Tools come from the project's `policy` and the characters' seat rules, not from the
   flow. A draft carries no `allowed_tools`, `host` or `code_tools`.
5. Hub data is production state. Drafts live in their own directory, never in `flows/`.
6. A run spends the shared LLM quota and takes a debate slot. The existing slot limit applies (HTTP 429 when full).

## 3. Draft format (SD3)

A draft is a team-shaped lineup without providers, hosts or tools:

```json
{
  "id": "api-review",
  "name": "API review",
  "kind": "debate",
  "lead": "backend_expert",
  "critics": ["security_critic", "api_critic"],
  "proposers": [],
  "close": "default_judge",
  "max_rounds": 2,
  "category": "development",
  "projects": ["code"]
}
```

| Kind | Seats the draft fills |
|---|---|
| `debate` | `lead`, `critics` (1-4), `proposers` (0-2), `close` |
| `panel` | `lead`, `proposers` (1-2), `close`; no critics |
| `verify` | `lead`, `critics` (1-4, checklist mode), `close` |
| `pipeline` | `stages` (2-4, characters in order), `lead`, `critics` (0-4), `close` |
| `versus` | `sides` (two `{character, stance}`), `exchanges` (1-2), `close` |

Each seat holds a character id. The hub turns the draft into a team dict (`expert`, `critic`, `judge`,
`proposers`, `flow: {kind, stages | sides, exchanges}`), with each member's providers taken from the character
(SD5). It then runs it through `parse_lineup` and `lineup_flow`, the path `team_to_flow` already uses, and checks
the result with `validate_flow` and `compile_flow`. The stored draft keeps both the short form and the converted
flow, so the dashboard shows the graph a promotion would save.

`projects` lists project kinds (`code`, `ops`, `design`), as in every flow, not project ids.

Limits: at most 20 drafts per workstation (SD6); ids follow `TEAM_ID_PATTERN` and are unique per workstation only;
`name` at most `MAX_NAME_CHARS`.

## 4. Session tools

| Tool | Hub path | Writes |
|---|---|---|
| `flow_catalog` | kinds, the seats each kind takes, characters with their home seat, limits, categories | no |
| `flow_list` | `GET /api/flows` trimmed: id, name, source, kind, runnable, projects, params | no |
| `flow_get` | one flow's seats and parameters, short form when it converts back | no |
| `flow_check` | converts and validates a draft; answers errors, warnings, calls per round, the graph | no |
| `flow_draft_save` | stores a draft under this workstation (checked first, refused when invalid) | draft store |
| `flow_draft_list` / `flow_draft_delete` | this workstation's drafts only | draft store |
| `flow_run` | `POST /api/debates` with `project`, `flow`, `seats`, `params`, `task?`, `started_by` | starts a run |
| `flow_run_get` | `GET /api/debates/{id}`, answered only when `started_by` is this workstation | no |

`flow_check` is the loop the model works in: draft, check, fix, check again, with no hub state changing.
`flow_run_get` answers a run another workstation (or the dashboard) started exactly as an unknown id, as
`review_get` does. Both get tools take `wait_sec` 0-15.

## 5. Promotion

The dashboard's Flow kinds tab gets a "Session drafts" list: workstation, draft id, kind, modified, checked
status and the converted graph in the read view. "Promote" saves the converted flow through `FlowStore.save` under
an id the operator confirms (default: the draft id). An id that already exists is refused unless the operator ticks
an explicit overwrite, and a builtin id is always refused. The promoted flow records
`promoted_from: {workstation, draft, at}`, and the draft is removed. "Delete" removes a draft. Both are Go
endpoints (`GET /api/flow-drafts`, `POST /api/flow-drafts/{ws}/{id}/promote`, `DELETE /api/flow-drafts/{ws}/{id}`)
over CLI flags, the same `configGet`/`configSave` pattern the flows use.

## 6. Runs

The sequence is in [session-flows-sequence.mmd](session-flows-sequence.mmd).

- `flow_run` takes the same fields as a flow start in the dashboard. Go's `validateFlow` and Python's
  `resolve_run` check them; seat swaps go through `swap_characters`, which refuses a character that cannot sit in
  the seat.
- `POST /api/debates` learns `started_by` (`session:<workstation>` only, checked like the review handler's), stored
  in the job summary and shown in the debate list.
- A session-started run acts on nothing by itself (SD4): no `auto:engine` apply after the jev gate, no
  ops `auto_run`, no batch auto release. Its actions wait in Approvals for a person.

## 7. Decisions

| # | Question | Decision |
|---|---|---|
| SD1 | Where a session's flow is saved | Decided 2026-11-25: a per-workstation draft store; a person promotes it in the dashboard. No overwrite of builtin or operator flows from a session. |
| SD2 | Whether a session may start runs | Decided 2026-11-25: yes, existing flows only, stamped `started_by: session:<ws>`, under the existing slot limit. |
| SD3 | Which flow format a session sends | Decided 2026-11-25: a kind and the characters in its seats (section 3); the hub builds the graph. |
| SD4 | Auto paths for session-started runs | Decided 2026-11-26: all off (`auto:engine`, ops `auto_run`, batch auto); a person approves every action. |
| SD5 | Providers of a draft's seats | Decided 2026-11-26: from the character (format 2 `providers`), else the hub's default order; a draft names none. |
| SD6 | Draft limits | Decided 2026-11-26: 20 per workstation, no expiry; a removed workstation's drafts are deleted with it. |

## 8. Phases

| Phase | Content | Check |
|---|---|---|
| SF1 | `flow_drafts.py`: short form to team dict to flow, the draft store under `flow-drafts/<ws>/`, CLI `--flow-draft-check`, `--flow-draft-save --workstation`, `--flow-drafts`, `--flow-draft-promote`, `--flow-draft-delete` | unit tests for every kind, refusals, promotion collisions; `testenv.isolate_data_dir()` |
| SF2 | Go endpoints for drafts; session ops `flow_catalog`, `flow_list`, `flow_get`, `flow_check`, `flow_draft_*` in all four layers | Go tests; link validator tests; end-to-end in `test_session_e2e.py` |
| SF3 | `started_by` on `POST /api/debates` and the job summary; SD4 switch; `flow_run`, `flow_run_get` | Go tests; e2e with a fake run; one real run on the hub sandbox with the operator's OK |
| SF4 | Dashboard: Session drafts list with promote and delete; `started_by` in the debate list; strings in `ko` and `en` | web tests; real browser on a local hub at desktop width and 390 px |

## 9. Implementation notes

- SF1 (2026-11-26): `cli/llm-agent/flow_drafts.py` (the short form, the conversion to a flow, the draft store under
  `flow-drafts/<ws>/`), the CLI flags `--flow-draft-check`, `--flow-draft-save --workstation`, `--flow-drafts`,
  `--flow-draft-promote [--promote-as NEWID] [--overwrite]` and `--flow-draft-delete` (the existing `--workstation`
  flag of the session group is reused), `config_ops` wrappers for the SF2 endpoints, `promoted_from` added to
  `flow_graph.FLOW_KEYS` with its shape check in `validate_flow`, and `session_registry.remove_workstation` deleting
  the workstation's drafts (SD6); 29 tests in `test_flow_drafts.py`. Deviations from the plan:
  - A missing draft answers NOT_FOUND (exit 3, HTTP 404) from promote and delete, the code every other delete uses;
    validation refusals stay exit 2.
  - `promote` refuses a builtin flow id by its file in the builtin directory, even with overwrite and even when a
    custom flow shadows that id, so a draft can never take a builtin id (claude review; the first version checked
    the stored source and let a shadowed id through).
  - SD5: format-2 character files carry no providers, so every seat of a converted draft has an empty provider list
    and runs in the hub's default order. A draft cannot name providers at all; a dict in a seat is refused.
  - The seat-count errors of the short form count a missing required list as zero ("critics: 1-4 characters, got 0"),
    so the message names the range instead of the absence.

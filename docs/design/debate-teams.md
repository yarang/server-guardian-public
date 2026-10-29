# Debate teams (lineups) design

English | [한국어](debate-teams.ko.md)

## Decision

The unit that runs in a debate is the **individual agent**; a **team is configuration that defines the role assignment (lineup)**.
Team-versus-team debates (each side debates internally and then speaks through a representative) are not adopted.

| Criterion | Team versus team | Individual agents + lineup (adopted) |
|---|---|---|
| LLM calls per round | about 10-15 | 3, or 3+N with a critic panel |
| Latency | up to 300 s per call, accumulating serially | the panel runs in parallel |
| Traceability | the team's internal agreement is blurred | every statement records its agent and provider |
| jev gate | judges a summarised representative opinion | judges the original statements |

Diversity of viewpoints comes from **per-role providers** and the **critic panel**.

## Structure

```
configs/teams/<id>.json ──TeamManager.load──▶ Lineup ──▶ DebateEngine.run_debate(team=<id>)
                                               │
                                               ├─ expert : Member(persona, providers)   single
                                               ├─ critics: [Member, ...]               1-4, parallel
                                               └─ judge  : Member(persona, providers)   single
```

- `cli/llm-agent/team_manager.py`: `Member`, `Lineup`, parsing and validation, `TeamManager` (file load and save)
- Two storage layers: the repository's `configs/teams` (builtin, read-only defaults) and `GUARDIAN_TEAMS_DIR` (default `<GUARDIAN_DATA_DIR>/teams`, that is `~/.guardian/data/teams`, custom). With the same id the custom one wins, and saving always writes to custom. Personas work the same way (`configs/personas` / `GUARDIAN_PERSONAS_DIR`, default `<GUARDIAN_DATA_DIR>/personas`).
- `DebateEngine`: takes the lineup and calls an individual agent per role. `_ask_critics` returns a list of results, so the engine does not need to know whether the critic is single or a panel.
- `LLMRunner.run_session_prompt(providers=[...])`: tries only the providers the member names, in that order. A provider is used when installed even if the execution host's default order (`GUARDIAN_LLM_PROVIDERS`) leaves it out. When none is installed: built-in fallback → round `UNVERIFIED`.

### Builtin teams (2026-10-18)

| id | Lineup | Rounds | Allowed tools |
|---|---|---|---|
| `default` | `domain_architect` / `critic_redteam` / `jev_mcp_decision`, execution-host default providers | default 3 | omitted (judge persona's setting) |
| `cross-review` | `domain_architect` / panel `critic_redteam` + `devops_specialist` / `jev_mcp_decision`, providers per role | 3 | `jev_mcp:run_tests`, `jev_mcp:apply_patch`, `jev_mcp:execute_code_action` |
| `fleet-ops` | `ops_sre` / panel `ops_safety_critic` + `ops_capacity_critic` / `ops_judge`, execution-host default providers | 2 | the 8 server tools (the 7 `ops:*` catalog actions and `jev_mcp:server_remediation`) |
| `fleet-provision` | `ops_provisioner` / panel `ops_provision_safety_critic` + `ops_dependency_critic` / `ops_provision_judge`, execution-host default providers | 2 | `ops:package_install`, `ops:package_remove`, `ops:restart_service`; plans, not single actions, carry package orders (see [fleet-provisioning.md](fleet-provisioning.md)) |
| `fleet-services` | `ops_service_engineer` / panel `ops_service_safety_critic` + `ops_service_cause_critic` / `ops_service_judge`, execution-host default providers | 2 | `ops:restart_service` |
| `fleet-mesh` | `ops_mesh_engineer` / `ops_mesh_risk_critic` / `ops_mesh_judge`, execution-host default providers | 2 | none (report only; see P8 in [server-ops-project.md](server-ops-project.md)) |
| `guardian-dev` | `guardian_lead_dev` (claude) / panel `guardian_security_critic` (codex, agy) + `guardian_qa_critic` (agy, codex) + `guardian_release_critic` (agy, codex) / `guardian_dev_judge` (claude) | 3 | `jev_mcp:run_tests`, `jev_mcp:apply_patch`, `jev_mcp:execute_code_action` |

`guardian-dev` is the team for developing Server Guardian itself: Claude drafts and judges, Codex and Gemini (`agy`) review. It is not the default team of any project; pick it with `--team guardian-dev`.

`fleet-ops` is the team for ops projects. It is the default team of the builtin ops project `fleet`; its personas and roles are described in chapter 14 of [server-ops-project.md](server-ops-project.md), "Ops team and LLM CLI skills". Without `configs/teams/default.json`, `default` still works as the lineup built into the code (`default_lineup()`).

### Rules

- **The expert and the judge are always a single agent.** The proposal and the approval each need one accountable author for the jev gate and the action approval flow to make sense.
- **Critic panel statements are concatenated verbatim** (`### Critique from <name>`). They are not summarised by an LLM, which would blur the evidence and add calls.
- **If any panel member falls back to built-in, the whole round is `UNVERIFIED`.**
- **A team can only narrow the judge's tools.** Effective tools = `allowed_tools` ∩ the judge persona's `mcp_capabilities`. `allowed_tools: []` means no tool proposals; omitted means the persona's setting.
  (2026-10-18: the project narrows the result once more. A code project keeps only code tools; an ops project keeps only the server tools listed in its `policy.allowed_actions` (`MCPClient.project_tools`). When nothing is left in an ops project, a warning is logged at debate start. Retired tools (`jev_mcp:git_commit_push`) are ignored with a warning.)
- Allowing a high-risk tool (`server_remediation`) gives a validation warning. Running it still needs a person's approval.

### Session keys

The primary key of `agent_sessions` is `(thread_id, role)`. To support panels without a migration:

- Single member: `role` as is (`Critic Agent`), so sessions of existing threads continue.
- Panel member: `role:persona` (`Critic Agent:devops_specialist`), which is why personas in a panel cannot repeat.
- Select one by prefix, e.g. `--attach <thread> --role "critic agent:dev"`.

### Reproducibility

- When a thread starts (or is rerun with `--team`), the lineup snapshot is stored in `threads.team_snapshot` (JSON) and in the thread's JSON/Markdown export.
- Existing databases get `ALTER TABLE threads ADD COLUMN team_snapshot` automatically at startup.
- Continuing with `--thread` and no `--team` **reuses the stored lineup**. With `--team`, that team replaces it and the snapshot is updated.

### Precedence

1. `run_debate(team=...)` / `--team`
2. The lineup stored with the thread
3. The project's default team (added 2026-10-18: `"team"` in the project JSON, e.g. `fleet-ops` for the builtin `fleet`)
4. The default lineup (`domain_architect` / `critic_redteam` / `jev_mcp_decision`, execution-host default providers)

(`DebateEngine._resolve_lineup`.) When the stored lineup is no longer valid (a persona was deleted since) or the project's team cannot be loaded, the default lineup is used and a warning is logged.
On top of that, the `expert_id` / `critic_id` / `judge_id` arguments override only that role's persona (with the execution host's default providers). Rounds: `--rounds` > the team's `max_rounds` > 3.

## Team file schema

```json
{
  "id": "cross-review",
  "name": "Cross-provider review (critic panel)",
  "expert": {"persona": "domain_architect", "providers": ["claude", "codex"]},
  "critic": [
    {"persona": "critic_redteam", "providers": ["codex", "agy"]},
    {"persona": "devops_specialist", "providers": ["agy", "codex"]}
  ],
  "judge": {"persona": "jev_mcp_decision", "providers": ["claude", "codex"]},
  "max_rounds": 3,
  "allowed_tools": ["jev_mcp:run_tests", "jev_mcp:apply_patch", "jev_mcp:execute_code_action"]
}
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Must equal the file name. Lowercase letters, digits, `-`, `_`, up to 64 characters |
| `expert`, `judge` | yes | `{"persona", "providers"}` or a persona id string |
| `critic` | yes | One member or a list of 1-4 |
| `proposers` | | Optional list of 0-2 extra drafters (added 2026-10-18, [multi-perspective.md](multi-perspective.md)). In the first round of a run they write drafts blind to the expert; ignored with a warning in ops and design projects. Personas must differ from the expert's and from each other |
| `providers` | | Order to try. Empty means the execution host's default order. `claude, codex, agy, opencode, ollama, llm` |
| `code_tools` | | Optional per member (added 2026-10-18). `"mcp"` lets a claude or codex member use the read-only repository tools (`repo_mcp.py`). Needs `GUARDIAN_REPO_MCP=1` on the execution host. Opt-in: the team editor shows an "Add repository tools" checkbox (off by default, field omitted when off). Only code projects use it; an ops or design project ignores it and logs a warning at debate start (2026-10-19) |
| `max_rounds` | | 1-10 |
| `allowed_tools` | | Upper bound of the judge's tools. Omitted means the persona's setting |
| `context` | | Default reference code `{"paths": [glob...], "ref": "HEAD"}` (added 2026-10-18, [debate-teams-api.md](debate-teams-api.md) section 5). Ignored with a warning in ops projects |
| `category` | | Goal of the team, for the Settings → Teams list (added 2026-10-19): `development`, `operations`, `design-docs` or `general`. Saving anything else is refused; a stored unknown value is a warning and shows under "Other". Not part of the lineup snapshot |
| `created`, `modified` | | ISO 8601 timestamps (added 2026-10-19). The engine stamps them on every save from the dashboard or `--team-save`: `modified` is now, `created` is kept from the stored team (also from the builtin one when a builtin is edited) and set on the first save. Builtin files carry the date of their first commit. Teams without a date sort last |

The Settings → Teams page lists teams as rows (name, source, id, a one-line summary, creation date) that open into the full lineup. By default they are grouped by goal and sorted newest created first; the sort can be changed to recently edited or name, and the grouping turned off.

Secrets (tokens) never go into team files. Credentials stay in `~/.guardian/llm.env` only.

### Persona page (2026-10-22)

Personas have their own dashboard page, `#/debates/personas` (a "Personas" link sits next to "manage teams" on the Debates page and in Settings → Personas). `#/debates/personas/<id>` edits one persona and `#/debates/personas/new` opens a blank form. The list shows every persona with a builtin/custom chip, searchable on name, id and domain and filterable by role. The editor is a full page, not a modal, and covers every field the hub accepts: id (new only), name, role (free text with the existing role labels as suggestions), domain, system prompt, `focus_areas` and `critique_focus` (one item per line) and `mcp_capabilities` (checkboxes over the same tool catalog the team editor uses, so no new endpoint). "Start from" copies an existing persona with the id cleared, and three starter templates (Expert, Critic, Judge; text in the i18n catalogs) fill a skeleton. A "Used by" section lists the teams whose expert, proposers, critics or judge reference the persona. Saving a builtin creates a custom override; deleting is for custom personas only and asks for confirmation. Validation errors from the hub agent stay on the page. The older editor dialog in Settings → Personas is kept for quick edits (it still caps the temperature at 1).

## Usage

```bash
python3 cli/llm-agent/guardian.py --teams                        # list + validation + installed on this node
python3 cli/llm-agent/guardian.py --debate "task" --team cross-review
python3 cli/llm-agent/guardian.py --debate "follow-up instruction" --thread <ID>  # reuses the stored lineup
```

## Next steps

1. **Dashboard integration**: see [debate-teams-api.md](debate-teams-api.md) (persona and team editing, running debates).
2. **Target scope**: add `target_group` (a node group) to teams to restrict `server_remediation`'s `target_host` to that group.
   (2026-10-18: implemented through the `targets` of ops projects rather than teams. See [server-ops-project.md](server-ops-project.md).)
3. **Team versus team**: only when real debate records show the panel is not enough, as a `TeamParticipant` with the same `ask` interface.

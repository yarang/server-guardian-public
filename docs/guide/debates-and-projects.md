# Debates, Flows, and Projects

English | [한국어](debates-and-projects.ko.md)

Back to [User Guide](README.md).

This module explains how to configure projects, run multi-agent debates, flows, Quick ask, Code Wiki builds,
whole-repository audits, action approvals, and branch publishing.

## 1. Project kinds

Every debate belongs to a project (built-in definitions live in `configs/projects/`; custom projects live in
`~/.guardian/data/projects/` on the hub and override built-ins with the same id):

| Kind | Target | Workspace & Wiki | Typical actions |
|---|---|---|---|
| `code` | A Git repository (e.g., built-in `server-guardian` or your own service repo) | Hub clone under `~/guardian-workspaces/<id>`, symbol index, Code Wiki | `apply_patch`, `execute_code_action`, `run_tests`, **Publish** |
| `ops` | Nodes and node groups (e.g., built-in `fleet`) | No repo or wiki | Cleanup catalog (`ops:*`), package plans, command sets, `server_remediation` |
| `design` | Architecture documents, proposals, and diagrams | No repo; versioned Markdown **Artifacts** | Versioned documents, Mermaid diagrams, DOCX/PPTX export |

Manage projects from **Settings > Flow setup > Projects** or CLI:

```bash
python3 cli/llm-agent/guardian.py --projects
python3 cli/llm-agent/guardian.py --project-sync all
python3 cli/llm-agent/guardian.py --project-sync my-service
```

For private Git repositories, configure Git credentials on the hub once (for example a token with
`git config --global credential.helper store`) and keep repository URLs credential-free in project configs.

## 2. Starting debates and flows

In a debate, an **Expert** drafts and consolidates, a **Critic** panel (1–4 critics in parallel) reviews the draft,
and a **Judge** delivers the verdict and proposes typed actions.

### Built-in teams
- **Code**: `default`, `cross-review`, `guardian-dev`
- **Ops**: `fleet-ops`, `fleet-provision`, `fleet-mesh`, `fleet-services`
- **Design**: `design-review`

### Running from CLI

```bash
# Start a new debate on a code project
python3 cli/llm-agent/guardian.py --debate "Find and fix race conditions in session_daemon.py" \
  --project server-guardian --team cross-review --context-path 'cli/llm-agent/session_*.py'

# Start using a saved flow graph and swap a character in a seat
python3 cli/llm-agent/guardian.py --debate "Review API auth changes" \
  --project my-service --flow code-review --seat critic_1=security_critic

# Continue an existing thread
python3 cli/llm-agent/guardian.py --debate "Address the second critic objection" --thread <THREAD_ID>

# Interactive REPL
python3 cli/llm-agent/guardian.py
```

### Flow editor
Under **Settings > Flow setup > Flows** (`#/settings/flows`), you can visually build or edit flows on a canvas:
- **Seats**: assign a character and a job (`write`, `critique`, `rewrite`, `rebut`, `summarise`).
- **Bundles**: run multiple seats in parallel.
- **Links and input modes**: control how each seat receives earlier outputs (`raw`, `condensed`, `structured`,
  `latest`).
- **Close**: every flow ends at one shared close (Judge, `jev` gate, and action checks).

List or manage teams, personas, and flows via CLI:

```bash
python3 cli/llm-agent/guardian.py --teams
python3 cli/llm-agent/guardian.py --personas
python3 cli/llm-agent/guardian.py --flows
```

## 3. Quick ask (solo agent)

When you want a fast, read-only answer from a single role without a Critic panel, Judge, or actions, use **Quick ask**
(**Workflows > Quick ask**, `#/debates/ask`) or `--agent-run`:

```bash
# Project-scoped read-only question
python3 cli/llm-agent/guardian.py --agent-run "How does the hub authenticate dashboard requests?" \
  --project server-guardian --provider claude

# General question (no project context)
python3 cli/llm-agent/guardian.py --agent-run "Compare systemd MemoryMax and MemoryHigh" \
  --general --provider claude
```

## 4. Evidence, Code Wiki, and whole-repository audit

### Code reading and evidence
Debate agents read repository files through ```` ```repo ```` blocks or the read-only MCP server `repo_mcp.py`,
assisted by the symbol index, repository map, and Go type facts (`tools/goindex`). Navigation aids are never treated as
evidence—only lines actually shown to the agent session pass citation checks.

### Code Wiki
Code Wiki pages are generated for people browsing the dashboard (**Workflows > Wiki**, `#/debates/wiki`, with **Read**,
**Files**, and **History** views). Refresh is manual:

```bash
python3 cli/llm-agent/guardian.py --code-wiki-list --project server-guardian
python3 cli/llm-agent/guardian.py --code-wiki-build --project server-guardian --stale
python3 cli/llm-agent/guardian.py --code-wiki-build --project server-guardian --all
```

### Whole-repository audit mode
Run a property audit across a repository (sharded read-only findings report, no edits):

```bash
python3 cli/llm-agent/guardian.py --repo-audit "Find every HTTP handler missing auth checks" \
  --project server-guardian --audit-scope 'apps/agent/*.go'
python3 cli/llm-agent/guardian.py --repo-audit-list --project server-guardian
python3 cli/llm-agent/guardian.py --repo-audit-run <AUDIT_ID> --project server-guardian
python3 cli/llm-agent/guardian.py --repo-audit-report <AUDIT_ID> --project server-guardian
```

## 5. Verdict, `jev` gate, actions, and Publish

1. **Verdict & `jev` gate**: when the Judge approves, the debate is filed as an ADR with `jev-mcp` for independent
   review. Only when `jev` passes (`APPROVED`) are proposed actions released; otherwise they stay `HELD`.
   ```bash
   python3 cli/llm-agent/guardian.py --jev-status [<THREAD_ID>]
   python3 cli/llm-agent/guardian.py --jev-recheck <THREAD_ID>
   ```
2. **Code actions**:
   - File edits (`apply_patch`, `execute_code_action` inside the project's clone) apply automatically once the `jev`
     gate passes (`decided_by: auto:engine`, unless `GUARDIAN_AUTO_APPLY=off`). They never push.
   - `run_tests` and actions requesting `run_test` wait for human approval.
   ```bash
   python3 cli/llm-agent/guardian.py --actions [<THREAD_ID>]
   python3 cli/llm-agent/guardian.py --show-action <ACTION_ID>
   python3 cli/llm-agent/guardian.py --run-action <ACTION_ID>
   python3 cli/llm-agent/guardian.py --reject-action <ACTION_ID>
   ```
3. **Publish (the only supported push path)**: pushes only files created or changed by judge actions to a new branch
   after `tests.default` passes (never force-pushes).
   ```bash
   # Preview plan and publish (prompts for 'yes')
   python3 cli/llm-agent/guardian.py --publish-thread <THREAD_ID> [--branch fix/my-branch]

   # View publication history or reset a dirty project workspace
   python3 cli/llm-agent/guardian.py --publications <THREAD_ID>
   python3 cli/llm-agent/guardian.py --workspace-reset <PROJECT_ID>
   ```
   After merging the published branch into your main branch, sync the hub clone with `--project-sync`.

4. **Office export from reports (CLI)**:
   ```bash
   # Keep a code/ops debate report as an artifact version
   python3 cli/llm-agent/guardian.py --report-keep --thread <THREAD_ID>

   # Convert an artifact version to Word or PowerPoint
   python3 cli/llm-agent/guardian.py --artifact-convert report.md --thread <THREAD_ID> \
     --artifact-format docx
   ```

## 6. Inspecting CLI debate sessions

Debate roles keep resumable CLI sessions on the hub (`agent_sessions` table, workdir `~/.guardian/llm-sessions`).

```bash
# List recorded sessions
python3 cli/llm-agent/guardian.py --sessions [<THREAD_ID>]

# Attach to a role's CLI session in tmux
python3 cli/llm-agent/guardian.py --attach <THREAD_ID> --role critic

# Prune sessions older than 30 days
python3 cli/llm-agent/guardian.py --prune-sessions 30
```

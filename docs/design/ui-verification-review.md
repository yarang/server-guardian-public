# Dashboard review for the verification layer: what goes, what moves, what comes (design)

English | [한국어](ui-verification-review.ko.md)

Status: proposed 2026-11-23. UD1, UD2, UD4, UD5 and UD6 approved by the operator the same day; UD3 (remove the desktop
export folder) approved 2026-11-24. U0-U3 deployed 2026-11-23 as `e9a939a`; X3 built 2026-11-24, not deployed.
Origin: DP1 of [verification-layer.md](verification-layer.md) (Guardian is a verification and control layer plus fleet
operations, not an agent development environment). This document checks the dashboard against that positioning:
features that no longer earn their place, configuration that sits among daily work, and the screens the quick review
needs. The menu itself (five destinations, `ui-flow-redesign.md`) is kept.

## Current layout (2026-11-23, `apps/web-desktop/src`)

| Destination | Tabs and sections |
|---|---|
| Overview | hub, SSH, approvals, debates panels |
| Nodes | nodes, groups, SSH, commands, command sets |
| Workflows (per project) | Debates, Quick ask, Resident agents, Flow kinds, Wiki (code) or Artifacts (design), Results; Personas on a hidden route `#/debates/personas` |
| Approvals | proposed actions and plans |
| Settings | hub connection; flow setup (Projects, Teams, Personas); execution environments (Workers); display (language, desktop export folder) |

Findings:

1. **Teams outlived their decision.** D7 of `flow-graph.md` (decided 2026-11-01): "A flow replaces team plus template
   ... The team layer goes away". The dashboard still has the team editor (Settings -> Teams, `TeamsSection.tsx` +
   `TeamDialog.tsx`, about 790 lines), a "Manage teams" link on Workflows and a team picker in the start form beside
   the flow picker. Two ways to choose a lineup for one debate.
2. **Personas are edited in two places.** The full editor page (`pages/personas/`, reached from Workflows) and a
   quick dialog in Settings (`PersonasSection.tsx` + `PersonaDialog.tsx`, about 330 lines) that says itself the full
   editor lives elsewhere.
3. **Configuration sits among daily work.** Flow kinds (the flow editor) and Personas are tabs or links of Workflows,
   the place where work is started and read; Projects and Teams are in Settings. Setup is split across two
   destinations.
4. **Workflows has six tabs** (seven with the quick review) and the 390 px width was never checked for them.
5. **Nothing points at verification.** Quick ask is the only fast path, and a debate is the only checked one; there is
   no place to hand Guardian a change and get findings.

## Proposal

### Removals

| # | What | Why | Precondition |
|---|---|---|---|
| X1 | Team editor (Settings -> Teams, `TeamsSection`, `TeamDialog`), the "Manage teams" link, the team picker of the start form | D7 already decided it; flows cover every team (all hub teams convert, G1) | the start form defaults to the project's flow. Checked 2026-11-23: not built. The engine resolves a project's `"team"` with the team manager only (`debate_engine.py`, `_resolve_lineup`, `project_team`), so D7's "a project's team key names a flow" needs an engine change and a project-editor field first (step U0). Team files and `--teams` stay for the CLI and the conversion |
| X2 | Quick persona dialog in Settings (`PersonasSection`, `PersonaDialog`) | duplicate of the full editor | none |
| X3 | Desktop export folder (`AutoExport`, `ExportFolderSection`, Settings -> display) | writes reports into a desktop folder automatically: producing and shipping files is not this layer's job | **ask the operator first**: whether it is used. Report downloads and Office export stay |

Kept on purpose: Wiki (people read code before judging a finding), Artifacts and design projects (reviewing documents
is verification), Office export and Mermaid (deployed and confirmed by the operator), Results, Approvals, every Nodes
tab, Resident agents. The legacy hash aliases stay (they cost a table).

### Moves

| # | What | From | To |
|---|---|---|---|
| M1 | Flow kinds (flow list and editor) | Workflows tab | Settings -> flow setup -> Flows |
| M2 | Personas (full editor) | hidden Workflows route | Settings -> flow setup -> Characters (the glossary word for a persona used as an identity) |

Settings -> flow setup becomes **Projects | Flows | Characters**. Old hashes (`#/debates/templates`,
`#/debates/personas...`) resolve to the new places, as the menu change of 2026-10-29 did.

### Additions (with V1 and V2 of the verification layer)

| # | What | Phase |
|---|---|---|
| A1 | Workflows tab **Quick review** (`#/debates/review`), first tab for code projects: project, range or pasted patch, findings by severity with citation chips, notes folded, the `REVIEW` chip and the "no Judge" line, "Run as debate" | V1c |
| A2 | Overview panel "Debates" becomes **Checks**: recent quick reviews and debates, and how many wait for a person | V1c |
| A3 | Approvals: a "Review" button on a resident patch; the findings shown beside the patch before the decision | V2 |

### Resulting Workflows tabs

- Code project: **Quick review | Debates | Quick ask | Wiki | Results | Resident agents**
- Design project: **Debates | Quick ask | Artifacts | Results | Resident agents**
- Ops project: **Debates | Quick ask | Results | Resident agents**

Six tabs at most, as today, with the quick review in place of Flow kinds. Resident agents stays last (it is not tied to
the selected project). The tab bar is checked at 390 px in the same step.

## Decisions

| # | Question | Recommendation | Decision |
|---|---|---|---|
| UD1 | Remove the team editor and the start form's team picker (X1)? | yes, after U0 | approved 2026-11-23 |
| UD2 | Remove the quick persona dialog (X2)? | yes | approved 2026-11-23 |
| UD3 | Desktop export folder (X3): keep or remove? | operator's call: remove if unused | approved 2026-11-24 (remove) |
| UD4 | Move Flow kinds and Personas into Settings -> flow setup (M1, M2)? | yes | approved 2026-11-23 |
| UD5 | Quick review as the first Workflows tab of code projects (A1)? | yes | approved 2026-11-23 |
| UD6 | Overview "Checks" panel (A2)? | yes, with V1c | approved 2026-11-23 |

## Order of work

0. **U0** engine: a project names its default flow (D7 of `flow-graph.md`), project editor field, ops watch unchanged
   (its team converts to a flow); tests.
1. **U1** removals X1 (after U0), X2, and X3 if decided; both catalogs lose the strings; tests updated.
2. **U2** moves M1, M2 with hash redirects.
3. **U3** = V1c: Quick review tab and the Overview panel.
4. Each step: `scripts/ci.sh web`, a real-browser check at desktop width and 390 px, one deploy after U3 with the
   operator's OK (the cloud session cannot reach the hub).

## 구현 메모

- **U0 (2026-11-23).** No new project key: D7 says the `team` key names a flow, and flow ids share the team id pattern,
  so `team` may now name a stored flow or a template. The engine (`DebateEngine._project_flow_run`) resolves it like
  `--flow` (no parameters, no seat swaps, the person's task) only when the request names no team or flow, the thread has
  no stored lineup and no per-role override is given; a key that names a team keeps the old team path unchanged. A flow
  that cannot start (wrong project kind, own task text) warns and falls back to the team path. The project dialog has a
  "Default flow" select (runnable flows of the kind without their own task text, plus the stored value if unlisted).
  Found on the way: the dialog used to drop `team` on save, so editing a project in the dashboard silently reset its
  lineup; it now keeps it.
- **Flow editor parity (2026-11-23), precondition of X1 found during U0.** The team editor offered a member's worker
  `host` and `code_tools: "mcp"`, the flow editor did not (the flow format had both). Seats (except the summariser) and
  the close now have both fields, with the team editor's rules (host field hidden without workers unless set).
- **U1 (2026-11-23).** X1 and X2 removed: `TeamsSection`, `TeamDialog`, `PersonasSection`, `PersonaDialog`, the start
  form's team picker and "Manage teams" link, and their strings. Without a chosen flow the form says which lineup runs
  (the project's default flow, else the hub default) and sends no team. "Run again" picks the flow of the team the
  debate ran with (teams are listed converted); with a flow chosen there is no thread field, as before for flows. Team
  context defaults (`context.paths` of a team) are no longer prefilled; the project's context paths still apply. X3
  untouched (UD3 open).
- **U2 (2026-11-23).** Settings -> flow setup is Projects | Flows | Characters (`#/settings/flows`,
  `#/settings/personas[/new|/<id>]`); `#/settings/teams`, `#/debates/templates` and `#/debates/personas...` redirect.
  The section id stays `personas` so old links keep working; the label is "캐릭터"/"Characters" (glossary word).
- **U3 (2026-11-23)** = V1c of `verification-layer.md`: A1 (Quick review tab, first for code projects) and A2 (Overview
  "Recent checks"); Workflows tab order as proposed (resident agents last). The flow gallery's title follows its new
  place ("흐름"/"Flows"), and the empty debate list no longer says "pick a team". U0-U3 were checked in a real browser
  on a local hub at 1280 px and 390 px (notes in `verification-layer.md`, V1c).
- **X3 (2026-11-24, UD3 approved).** The whole desktop export folder is removed, not only the automatic part: the folder
  chooser was the only way to set the root the manual "Save to folder" buttons used, so the report view, the Results tab
  and the Artifacts tab lose their folder buttons too. Removed: `AutoExport`, `ExportButton`, `ExportFolderSection`,
  `lib/exportFolder.ts`, `lib/exportThread.ts`, `lib/autoExport.ts`, their tests and strings, and in the shell
  `src-tauri/src/export.rs`, its ten commands and `tauri-plugin-dialog` (only it used the dialog; four crates left
  `Cargo.lock`, no version changed). `inDesktopShell` moved to `lib/shell.ts`. The shell keeps `save_download`
  (Markdown into Downloads); browser downloads and the Office export are unchanged. The tauri CI section could not run in
  the cloud container (no GTK); it runs on the Mac and regenerates `src-tauri/gen/schemas` without the dialog entries.

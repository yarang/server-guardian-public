# Web dashboard UI flow redesign

English | [한국어](ui-flow-redesign.ko.md)

Status: accepted (2026-10-07). Scope: `apps/web-desktop`. Sections 1-4 are the plan as accepted; section 5 records
how it was built and what changed later ("Later changes" describes the current navigation).

## 1. Current state (as of 3d00cd9)

Six flat routes, one hash segment each (`#/fleet`, `#/ssh`, `#/assistant`, `#/debate`, `#/batch`,
`#/terminal`). `App.tsx` remounts the page on every navigation (`<div key={route}>`).

| Route | What it holds | Data it loads |
|---|---|---|
| Fleet Overview | hub metrics (5 s poll), node inventory with group-by/filter, node group CRUD, fleet diagnosis (auto-run on open) | metrics, servers, groups, diagnoseFleet |
| SSH Security Audit | fleet sshd audit (group summary, rules-by-node heatmap, selected node), hub-local audit in a disclosure | fleet ssh, groups, hub ssh security/config/keys |
| Assistant | fleet diagnosis summary, hub-only SSH score, "Debate this" hand-off | diagnoseFleet, hub ssh security, groups |
| Persona & Debate | projects (selects the project), personas, teams, debate console + job + recent list, proposed actions, action dialog | personas, teams, projects, debates, actions |
| Batch Execution | target (fleet / group / node), presets, streamed run, per-node table | servers, groups |
| Remote Terminal | target (local / node), command history | servers |

### Problems

1. **No flow, only a list.** The work loop is observe -> analyse (debate) -> approve -> act, but the
   menu mixes observation, action and configuration at one level. Nothing tells the operator what
   needs attention now (offline nodes, failing SSH rules, actions waiting for approval).
2. **Duplicated work.** The fleet diagnosis probes every node on Fleet open *and* on Assistant
   open; servers and groups are fetched again on four pages. Each navigation re-runs it because the
   page remounts.
3. **Assistant is a thin bridge.** It repeats Fleet's diagnosis and shows a hub-only SSH score next
   to fleet numbers (it needs a scope note to avoid being misread). It is the only place a debate can
   be started from findings.
4. **Persona & Debate is overloaded.** Six concerns stacked vertically. Rarely changed configuration
   (projects, personas, teams) sits above the primary task (console); the approval queue, the most
   time-sensitive human step, is at the bottom. The selected project is page-local and resets to
   `server-guardian` on every visit.
5. **No entity pages.** A node cannot be opened. From an offline node or a failing SSH node there is
   no path to its audit, a shell on it, or a debate about it. Debates and actions cannot be linked.
6. **State is lost on navigation.** Leaving Batch cancels the stream watcher; leaving Debate drops
   the watched job; Terminal history is gone. The router ignores everything after the first segment,
   so nothing is deep-linkable.
7. **Terminal and Batch overlap.** Batch already runs on a single node; Terminal only adds the
   local agent node.
8. **Mobile.** TabBar carries six entries.

## 2. Target information architecture

Seven areas, grouped by the work loop. Labels stay in English (the UI language).
(2026-10-18: the dashboard copy is now Korean by default, with English as a choice in Settings;
see [dashboard-i18n.md](dashboard-i18n.md).)

| Group | Area | Route | Replaces |
|---|---|---|---|
| Monitor | Overview | `#/overview` (default) | Assistant, top of Fleet |
| Monitor | Nodes | `#/nodes`, `#/nodes/:id` | Fleet inventory + diagnosis |
| Monitor | Security | `#/security` | SSH Security Audit |
| Respond | Debate | `#/debate`, `#/debate/:jobId` | console + recent of Persona & Debate |
| Respond | Approvals | `#/approvals`, `#/approvals/:id` | Proposed actions panel + action dialog |
| Respond | Run | `#/run?target=...` | Batch Execution + Remote Terminal |
| Configure | Settings | `#/settings/:section` | agent endpoint dialog, projects, personas, teams, node groups |

Legacy hashes keep working: `fleet` -> `nodes`, `assistant` -> `overview`, `ssh` -> `security`,
`batch` / `terminal` -> `run`.

### Overview (home)

Answers "what needs attention now". Cards, each with one primary next step:

- Fleet reachability (online x / y, offline names) -> open Nodes filtered, or *Debate this*.
- Hub metrics (compact CPU / memory / disk / load).
- Fleet SSH (average score, failing nodes) -> Security, or *Debate this*.
- Approvals waiting (count, oldest) -> Approvals.
- Running / recent debates -> Debate job.

*Debate this* keeps the existing prefill hand-off but scopes the task to the card it came from.

### Nodes

Inventory table with the live probe merged in (status, uptime, load, memory, disk), group-by and
group filter as today. A row opens `#/nodes/:id`: probe result, the node's SSH audit row and rules,
and actions: *Open shell* (`#/run?target=<id>`), *Debate this node*. Group CRUD moves to Settings;
Nodes only filters by group.

### Security

The current fleet SSH audit unchanged in content; node names link to `#/nodes/:id`. The hub-local
view stays in its disclosure.

### Debate

Project switcher in the page header (persisted per browser). Body: start form, then the watched job
(log and result), then recent debates. `#/debate/:jobId` opens a job directly. Personas, teams and
projects are edited in Settings; the start form only picks a team.

### Approvals

The proposed-actions list as the main content, filtered by project and thread. `#/approvals/:id`
opens the existing action dialog content as a page section (run / reject / re-check unchanged,
confirmations unchanged). The nav item shows the count of actions waiting for a decision.

### Run

One target picker: local agent node, a node, a group, the whole fleet. Single targets behave like
the terminal (history kept); group / fleet runs keep the confirmation dialog, progress meter and
per-node table. Presets stay. `?target=` preselects (from a node page).

### Settings

Sections: Agent endpoint (and sign-out), Projects, Personas, Teams, Node groups. Existing dialogs
are reused unchanged.

## 3. Cross-cutting changes

- **Router with params.** `parseHash` returns `{ route, params, query }`; `hrefFor(route, ...)`
  builds links. Unknown hashes fall back to Overview.
- **Shared hub data.** Servers, groups and the latest fleet diagnosis live in an app-level store with
  the time measured; Overview and Nodes read the same probe and offer *Re-run* instead of probing on
  every visit.
- **Long-running work survives navigation.** The batch stream and the watched debate job are owned
  above the page; returning to the page shows the live state. Pages are no longer remounted just
  because the route changed.
- **Navigation.** Sidebar shows the three groups with headings; the approvals count is a badge.
  Mobile TabBar shows Overview, Nodes, Debate, Approvals, Run; Security and Settings are reached from
  the header menu.

## 4. Delivery phases

Each phase leaves the app working, tests green, and is committed separately.

1. **Foundation.** Router with params and legacy aliases; grouped navigation; shared store for
   servers / groups / diagnosis; stop remounting pages on route change. Pages unchanged otherwise.
2. **Overview and Nodes.** New Overview (absorbs Assistant); Nodes with merged probe and node detail;
   Security links nodes.
3. **Run.** Merge Terminal into Batch as Run with `?target=`; keep the run alive across navigation.
4. **Debate, Approvals, Settings.** Split the debate page; Approvals page with badge and
   `#/approvals/:id`; Settings with the configuration sections; persisted project choice; keep the
   watched job across navigation.

## 5. Implementation notes

Choices that differ from, or fill gaps in, the plan above.

Phases 1-3:

- `fleet` is an alias of `nodes`.
- Run's default target is the local agent node (it needs no confirmation).
- A node page has a single "Run on this node" link to `#/run?target=<id>`.
- Overview loads the fleet SSH audit itself; it is not part of the shared hub store.
- Heatmap row labels on Security are not links.

Phase 4:

- **Selected project** is an app-level store (`state/projectStore.tsx`), persisted in localStorage
  (`guardian.project`). A stored id missing from the loaded project list falls back to
  `server-guardian` without overwriting the stored value (a list that is temporarily unreachable
  never forgets the choice). The project list itself lives in this store and is shared with Settings.
- **Approvals page uses the action dialog over the list**, not a separate detail page.
  `#/approvals/:id` renders `ActionDialog` above the list; closing navigates to `#/approvals`.
  The run / reject confirmations and the lock while running are therefore unchanged. The Debate
  result's action buttons navigate to `#/approvals/<id>` (no dialog on Debate).
- **Approvals ordering**: always PROPOSED, then HELD, then decided; plus a local status filter
  (All / Awaiting decision / Held / Decided), default All.
- **Badge and Overview card share one fetch** (`state/approvalsStore.tsx`): 30 s poll, global Refresh,
  project switch, and `refresh()` after run / reject / re-check. The list on the Approvals page still
  fetches its own (thread-filtered) view, so that page makes one extra request.
- **Watched debate job** is owned by `state/debateStore.tsx` (together with the recent-debates list).
  Starting or opening a job updates the URL to `#/debate/<id>`; the URL is the single source for
  opening a job. The start-form fields themselves are page-local and reset on leaving Debate.
- **Settings** sections are a SegmentedControl over `#/settings/<section>`; each section fetches its
  own data when shown. "Change endpoint or sign out" is still the single existing dialog (no separate
  sign-out button). Projects lost their "Use" button; selection is the switcher only.
- **Nodes** keeps a "Manage groups" link to `#/settings/groups` instead of the group editor.

### Phase 5: group management flow

Group CRUD stays in Settings -> Node groups; Nodes and Node detail now feed it. All edits go
through `POST /api/groups` (the hub validates and its error is shown as returned) and then reload
the shared groups in `hubStore`, so Nodes, Run, Security and Node detail agree.

- **Nodes multi-select**: a checkbox column plus "Select all visible". A toolbar shows "N selected"
  with *Create group from selection* (the new-group `GroupDialog`, started with `initialInclude`;
  name empty), *Add to group...* and *Clear selection*. The selection is pruned to visible rows when
  a filter changes. *Add to group* shows what will change first: ids added to `include`
  (deduplicated) and ids removed from that group's `exclude`.
- **Node detail membership**: a "Group membership" panel lists each group with how the node is in
  it. Origin comes from the hub's group list (`members` are resolved): id in `include` -> "added",
  otherwise a member matched by rules -> "by rule". *Remove* of an added member drops it from
  `include`; of a rule match adds it to `exclude` ("excluded from rule match"); both confirm first.
  If the node is still a member after saving (it is picked and also matches a rule), the result says so.
- **Group summary on Nodes** (`?group=<id>`): name, description, member count, definition,
  reachability from the shared diagnosis (online x / y, offline names, probe age; "not probed" without
  one) and the members' fleet SSH summary (one cached `fetchFleetSSHAudit(memberIds, false)`).
  Actions: Run on this group (`#/run?target=group:<id>`), Debate this group (`buildGroupDebateTask`
  prefill), Edit group (`#/settings/groups?edit=<id>`, which opens the dialog; closing clears the query).
- **Settings list**: group names link to `#/nodes?group=<id>`.

### Later changes (2026-10-12 onward)

After phase 5 the navigation was consolidated further (commits "consolidate navigation into Fleet,
Debates, Approvals and Settings" and "move node groups from Settings to a Fleet Groups tab"). Current
state in `apps/web-desktop/src/router.ts`, `App.tsx` and `pages/FleetPage.tsx`:

- **Four destinations**: Fleet (`#/fleet`, default), Debates (`#/debates`), Approvals
  (`#/approvals`), Settings (`#/settings`). The sidebar and the mobile tab bar list the same four;
  there are no group headings and no More tab.
- **Fleet tabs** (first path segment, kept in the URL): Summary (`#/fleet`, the former Overview),
  Nodes (`#/fleet/nodes`, node page `#/fleet/nodes/<id>`, registration `#/fleet/nodes/new`), SSH audit
  (`#/fleet/ssh`, the former Security), Commands (`#/fleet/commands?target=...`, the former Run) and
  Groups (`#/fleet/groups`, `?edit=<id>` opens a group).
- **Debates** merges the debate console and the debate-agent monitor: one list with the selected debate
  beside it (`#/debates/<id>`), and a Wiki view (`#/debates/wiki[/<page>]`) for projects with a
  repository.
- **Settings sections**: Hub connection (`endpoint`, including the language choice), Projects,
  Personas, Teams. Node groups moved to the Fleet Groups tab; `#/settings/groups` redirects there with
  its query kept.
- **Legacy hashes** keep working with params and query: `overview`, `assistant`, `more` -> Fleet;
  `nodes` -> Fleet Nodes; `security`, `ssh` -> Fleet SSH audit; `run`, `batch`, `terminal` -> Fleet
  Commands; `agents`, `debate` -> Debates (`#/agents?run=<id>` selects that debate). Unknown hashes
  fall back to the Fleet summary.
- **Badges**: Fleet shows "Running" while a group or fleet run is in progress, Approvals shows the
  number of actions waiting (plus "Running" during an Execute), Debates shows "Running" during a
  Publish.

# Dashboard Guide

English | [한국어](dashboard.ko.md)

Back to [User Guide](README.md).

The dashboard is served directly by the hub agent on port `9870` (`127.0.0.1` and the hub's Tailscale IP, never a
public IP) and is also packaged as a Tauri 2 desktop app (`apps/web-desktop`).

## Signing in and connecting

1. Open `http://<hub-tailscale-ip>:9870/` in your browser (or launch the desktop app and set the hub URL).
2. Enter the hub token — the `AGENT_TOKEN` value from `~/.guardian/agent.env` on the hub.
3. The hub exchanges the token for an `HttpOnly` session cookie. To switch hubs or sign out, use the **Hub connection**
   button in the header or **Settings > Hub connection**.

The dashboard opens in **Korean** by default. Switch between Korean and English under **Settings > Display** (saved per
browser and applied on reload).

## Navigation overview

The dashboard is organized around five main destinations:

| Destination | Route | What you do here |
|---|---|---|
| **Overview** | `#/overview` | Check what needs attention now: fleet reachability, hub CPU/memory/disk, SSH audit summary, pending approvals, and recent debates for the selected project |
| **Nodes** | `#/fleet` | Inspect nodes, manage node groups, run fleet SSH audits, execute commands, and manage command sets |
| **Workflows** | `#/debates` | Start and follow debates, run Quick ask, monitor resident agents, browse the Code Wiki / Artifacts, and read Results |
| **Approvals** | `#/approvals` | Review, execute, or reject proposed actions and multi-node plans |
| **Settings** | `#/settings` | Configure hub connection, projects, flows, characters, execution environments (workers), and display options |

## 1. Overview (`#/overview`)

Overview gives a compact operational summary:
- **Reachability**: online vs. offline node count from the last fleet diagnosis, with quick links to offline nodes or
  starting an ops debate on the fleet.
- **Hub metrics**: live CPU, memory, and disk usage polled every 5 seconds.
- **SSH audit**: fleet average score and nodes with failing rules.
- **Approvals & Debates**: pending approvals and the 5 most recent debates for the currently selected project.

## 2. Nodes (`#/fleet`)

Five tabs cover all fleet operations:

- **Nodes (`#/fleet/nodes`)**: filter by status (`all` / `online` / `offline`) or group, toggle detail columns (Arch,
  Mesh IP, Probe), inspect hub host processes, approve or remove pending tailnet mesh-join nodes, register a new node
  (`#/fleet/nodes/new`), or click any node to open its detail page (`#/fleet/nodes/<id>`).
- **Groups (`#/fleet/groups`)**: create and edit dynamic or static node groups with live member previews.
- **SSH audit (`#/fleet/ssh`)**: run or refresh the fleet SSH security audit (`SEC-SSH-001`..`005`) and inspect per-node
  effective `sshd` settings and authorized key fingerprints.
- **Commands (`#/fleet/commands`)**: run an ad-hoc shell command or a saved command set against one node, a node group,
  or `all`, with streamed output and execution history.
- **Command sets (`#/fleet/commandSets`)**: view, create, edit, and confirm reusable command sets used by batch
  execution.

## 3. Workflows (`#/debates`)

Select the active project using the **Project switcher** at the top of the screen. Tabs adapt to the project kind:

- **Debates (`#/debates`)**: open **New debate** to launch a debate (pick a flow, or leave it on the project's default
  flow; optionally swap seat characters, set max rounds, or pass `--context-path` globs). While running, the live monitor shows each agent lane,
  retrieval steps, and timeline. When finished, view the verdict, `jev` gate result, proposed actions, **Continue**,
  **Rerun**, and **Publish** (for code projects). Toggle **Show closed** to include closed threads.
- **Quick review (`#/debates/review`, code projects, first tab)**: hand Guardian one change (a commit range `BASE..HEAD`
  or a pasted patch) and get findings from one read-only reviewer, labelled `REVIEW` (advisory, no Judge, no actions).
  Every finding is checked against the line it quotes; findings off the changed lines are set aside as notes. **Run as
  debate** sends the change and the findings to a debate for the shared close. CLI: `guardian.py --review --project ID
  --range BASE..HEAD`; API: `POST/GET /api/reviews`.
- **Quick ask (`#/debates/ask`)**: ask a single role a read-only question (`SOLO`, no judge, no actions). Supports both
  project-scoped questions and **general mode** (no project context). Pick any installed provider on the hub.
- **Resident agents (`#/debates/residents`)**: read-only hub-wide monitor of persistent Tier-1 resident agents, their
  recent turns, mailbox metadata (click a message to read its secret-masked body), task board, and store events. Polls
  every 10 seconds while visible.
- **Wiki (`#/debates/wiki`, code projects only)**: browse the project's generated Code Wiki with **Read**, **Files**,
  and **History** views, `[[page]]` cross-links, and manual wiki rebuild controls.
- **Artifacts (`#/debates/artifacts`, design projects; reachable by link for all kinds)**: inspect versioned Markdown
  reports and diagrams produced by debates.
- **Results (`#/debates/results`)**: consolidated list of debate reports and artifacts across threads for the selected
  project.

### Office export and diagrams

- Mermaid diagrams in reports render inline and include a **Save `.mmd`** button.
- On `code` and `ops` debate reports, click **Keep as document** to snapshot `report.md` as an artifact version (kept
  30 days by default).
- In the **Artifacts** viewer, convert any stored Markdown version to **Word (`.docx`)** or **PowerPoint (`.pptx`)**
  with **Make Word document** / **Make PowerPoint**.

## 4. Approvals (`#/approvals`)

Lists proposed **plans** (multi-node provisioning or batch execution) and individual **actions** for the selected
project, with items awaiting a decision first.

- Click any action (`#/approvals/<id>`) or plan (`#/approvals/plan_<id>`) to inspect its preview, diff, drift check,
  and `jev` / critic vote status.
- Running an action asks for confirmation; **high-risk** actions require typing the action number before execution.
- Dashboard decisions are recorded in the audit history as `dashboard@<IP>`.
- If the hub has `GUARDIAN_UI_ACTIONS=off`, the dashboard stays read-only for all run/reject/publish/close operations
  and displays the equivalent CLI command in the `403` notice.

## 5. Settings (`#/settings`)

Settings has four top-level sections:

- **Hub connection (`#/settings/endpoint`)**: view the connected hub URL, change hub URL, or sign out.
- **Flow setup (`#/settings/flowConfig`)**: switch between **Projects** (`code`, `ops`, `design` definitions, clone/sync,
  ops watch and batch policies, the **default flow** of debates started without one), **Flows** (`#/settings/flows`:
  the flow graph editor (`@xyflow/react`) with seats, parallel bundles, links, input modes (`raw`, `condensed`,
  `structured`, `latest`), the optional summariser and the shared close; per seat the providers, the worker host and
  repository tools) and **Characters** (`#/settings/personas`: inspect or create agent characters, run **Suggest with
  AI**, check persona lint warnings). Teams are listed as flows; the team editor was removed on 2026-11-23
  ([dashboard review](../design/ui-verification-review.md)), team files stay for the CLI. Old hashes
  (`#/settings/teams`, `#/debates/templates`, `#/debates/personas`) still open the new places.
- **Execution environments (`#/settings/workers`)**: inspect worker-capable Tier-1 nodes, protocol version, installed
  providers, and concurrency (`max_jobs`).
- **Display (`#/settings/display`)**: change UI language (Korean / English). (The desktop app's export folder was removed
  on 2026-11-24; the desktop app saves Markdown into Downloads, other files download from a browser.)

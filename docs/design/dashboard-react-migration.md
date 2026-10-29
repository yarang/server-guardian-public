# Dashboard React migration and design system (design)

English | [한국어](dashboard-react-migration.ko.md)

> 2026-10-18: this migration is done. The screens were later reorganised by [ui-flow-redesign.md](ui-flow-redesign.md) (see the notes in the "Structure" table), and the copy moved to per-language catalogs ([dashboard-i18n.md](dashboard-i18n.md)). The text below is the design at the time of the migration.

## Goals

Rebuild `apps/web-desktop` (vanilla TypeScript, about 4,100 lines, innerHTML string rendering) as a React 18 app,
and build every screen from components of the design system `@server-guardian/ui` (`packages/guardian-ui`).

- Consistency: the same tokens (`--sg-*`), fonts (Inter with IBM Plex Sans KR for Hangul, JetBrains Mono; see [dashboard-notion-theme.md](dashboard-notion-theme.md)) and status colour rules (`ok/warn/crit/info/idle`)
- Security: React escapes text by default, so the innerHTML-based XSS surface goes away. `dangerouslySetInnerHTML` is not used
  (2026-10-18: only `MarkdownDoc.tsx`, which renders Markdown documents (reports, wiki), puts HTML made by marked and Mermaid SVG into `innerHTML`, after DOMPurify (`lib/report.ts`) has sanitised them. `dangerouslySetInnerHTML` is still not used.)
- Honesty: keep the principle of the 2026-10-04 hotfix. API failures are shown as errors and never replaced with fake data

## Structure

| Item | Decision |
|---|---|
| Dependencies | `"@server-guardian/ui": "file:../../packages/guardian-ui"`, `react`/`react-dom` 18. The design system is built before the app (`prebuild`). Vite `resolve.dedupe: ['react','react-dom']` prevents loading React twice |
| Reuse | `src/api.ts` (hub API, auth) and `src/types.ts` are kept as they are |
| Entry point | `src/main.tsx` → `<App>` inside `<GuardianRoot>` |
| Navigation | `Sidebar` on desktop, `TabBar` below 760px (the design system switches). The URL hash (`#/fleet` and so on) keeps the tab state |
| Screens | `FleetPage`, `SshPage`, `AssistantPage`, `DebatePage` (teams, personas, debates, actions), `BatchPage`, `TerminalPage` in `src/pages/` (2026-10-18: there are now four routes: `#/fleet` (tabs: summary, nodes, SSH, commands, groups), `#/debates`, `#/approvals`, `#/settings` (hub connection, projects, personas, teams). `AssistantPage`, `BatchPage`, `TerminalPage` and `DebatePage` are gone; `OverviewPage`, `NodesPage`, `RunPage`, `DebatesPage`, `ApprovalsPage`, `SettingsPage` and others took over their roles. See `src/router.ts` and [ui-flow-redesign.md](ui-flow-redesign.md)) |
| Data | Small hooks in `src/hooks/` (`useHubData`, polling). Failures are shown on screen as an `{error}` state (2026-10-18: data that several screens share is held by the stores in `src/state/`) |
| Auth | Keep the existing flow (HttpOnly cookie on the same origin, token in Tauri). The sign-in dialog and the agent address setting are built from the design system's `TextField`, `Button` and `Panel` |
| Fonts | The design system ships the woff2 files itself, so Google Fonts is removed and googleapis/gstatic are dropped from the CSP (`apps/agent/ui.go`, `tauri.conf.json`) |
| Tests | Vitest + Testing Library (jsdom): per-screen rendering, error display, and the confirmation step of action approval |
| Removed | `main.ts`, `debate.ts`, `actions.ts`, `icons.ts`, `index.css`, `escape.ts` and their tests (replaced by React's escaping) |

## AI Multi-Agent Center → Assistant

In the current tab only the diagnosis numbers are real; the "AI summary opinion" and the proposed actions are fixed sentences in the code. During the migration:

- Show real data only: the `fleet/diagnose` result (online count per group, duration), the SSH security score and rules
- Delete the fixed copy and the hard-coded actions
- A "Debate this" button pre-fills the task input of the Debate screen. Analysis and actions go through a real LLM debate, the jev gate and the approval flow

(2026-10-18: the Assistant tab was removed later by the UI flow redesign. The fleet, node and group screens pre-fill a debate with `setDebatePrefill` (`src/state/debatePrefill.ts`).)

## Phases

1. App skeleton (dependencies, entry point, navigation, auth, shared hooks) and the Fleet, SSH, Assistant, Batch and Terminal screens
2. Debate screen (persona and team editing, running and viewing debates, code bundles, results, action approval and diff)
3. Remove the old vanilla files, clean up the CSP, check that the install script builds the design system first, verify in a local browser, deploy

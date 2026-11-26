# Server Guardian UI — how to build with it

Dark graphite ops console for Server Guardian (fleet monitoring, SSH audits, AI debates whose proposed actions need human approval). Components live on `window.GuardianUI`.

## Setup: always wrap in GuardianRoot
`GuardianRoot` sets the warm paper ground (`--sg-bg`), text color and the IBM Plex Sans KR font. Without it, components render on a white page in a fallback font and look broken. Wrap the whole screen once:

```jsx
const { GuardianRoot, Panel, NodeTable, StatusChip, Button } = window.GuardianUI;

<GuardianRoot>
  <div style={{ padding: 'var(--sg-s-10)', display: 'grid', gap: 'var(--sg-s-10)' }}>
    <Panel title="Nodes" actions={<Button variant="secondary" size="sm">Run fleet probe</Button>}>
      <NodeTable groups={[{ name: 'Site B Cluster', summary: '2 nodes', nodes: [
        { name: 'site-b-ec1', arch: 'x86_64', tier: 'T2', status: 'online', cpu: 71, mem: 83, disk: 91, load: 1.92, sshScore: 62 },
        { name: 'edge-mini', arch: 'arm64', tier: 'T2', status: 'offline' },
      ] }]} />
    </Panel>
    <StatusChip tone="ok">APPROVED</StatusChip>
  </div>
</GuardianRoot>
```

## Styling idiom: tokens, not classes
Use library components for every control. For your own layout glue use inline styles with `var(--sg-*)` tokens; never hard-code hex colors and never invent `sg-*` class names (the `sg-` classes are the components' internals).

| Family | Tokens |
|---|---|
| Surfaces | `--sg-bg`, `--sg-surface-sunken`, `--sg-surface`, `--sg-surface-raised`, `--sg-surface-hover`, `--sg-surface-selected`, `--sg-code-bg` |
| Lines | `--sg-border`, `--sg-border-subtle`, `--sg-border-strong` |
| Text | `--sg-text`, `--sg-text-secondary`, `--sg-text-muted` |
| Accent | `--sg-accent`, `--sg-accent-hover`, `--sg-on-accent`, `--sg-focus-ring` |
| Status (each has `-bg` and `-border` tints) | `--sg-ok`, `--sg-warn`, `--sg-crit`, `--sg-info`, `--sg-idle` |
| Spacing | `--sg-s-1`…`--sg-s-11` = 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32px |
| Radius | `--sg-r-tag` 4, `--sg-r-sm` 6, `--sg-r-md` 8, `--sg-r-card` 10, `--sg-r-panel` 12, `--sg-r-dialog` 14, `--sg-r-pill` |
| Type | `--sg-font-sans`, `--sg-font-mono` (host names, commands, numbers), `--sg-font-size` 14px |

Status meaning is fixed: `ok` healthy/APPROVED/LOW, `warn` degraded/DISPUTED/HELD/MEDIUM, `crit` failing/HIGH/offline-critical, `info` running/PROPOSED, `idle` unknown/offline. Components take these as `tone`.

## Domain rules the components encode
- Proposed actions are never auto-run: show `ActionCard` (PROPOSED/HELD get Approve/Reject) and confirm HIGH risk with `ConfirmDialog` type-to-confirm.
- Debate outcomes read as a `VerdictChain` (LLM judge → jev-mcp → Gate).
- Wide data: `NodeTable` needs ~940px and scrolls horizontally below that; `Heatmap` cells carry ✓ ! ✕ glyphs so status is not color-only.
- Navigation: `Sidebar` (hidden under 760px) on desktop, `TabBar` (shown under 760px) on phones.

## Where the truth lives
Read `styles.css` and its imports (`_ds_bundle.css` holds every token and component style) before styling, and each component's `<Name>.prompt.md` / `<Name>.d.ts` for props and examples.

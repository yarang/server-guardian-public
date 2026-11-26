# design-sync notes — Server Guardian

## Setup
- DS package: `packages/guardian-ui` (`@server-guardian/ui`), React 18 + Vite 8 library build, plain CSS with `--sg-*` tokens.
- Build first: `cd packages/guardian-ui && npm ci && npm run build` (two Vite builds: JS lib + stylesheet; then `scripts/strip-woff.mjs` drops .woff fallbacks; then `tsc` emits .d.ts).
- Converter run from repo root with `--node-modules packages/guardian-ui/node_modules --entry ./packages/guardian-ui/dist/index.js` (the package is not installed under node_modules/, so `--entry` is required).
- Render check: playwright 1.50.1 in `.ds-sync/` matches the cached chromium build 1155; `chromium-headless-shell` 1155 had to be installed separately (`npx playwright install chromium-headless-shell` from `.ds-sync/`).

## Gotchas found during verification
- Every component needs `GuardianRoot` around it for the warm paper ground and fonts → `cfg.provider = GuardianRoot`.
- Meter fill was a `<span>` without `display:block`, so its width/height were ignored and bars looked empty. Fixed in `src/styles/components.css`. jsdom tests cannot catch this class of bug — only screenshots do.
- NodeTable rows need ~940px; the default 900x700 capture viewport clipped the SSH column → `overrides.NodeTable = {cardMode: column, viewport: 1040x460}`; inner `min-width` raised to 940px; per-cell meter labels are screen-reader-only inside table rows.
- `guidelinesGlob: []` — the per-component `docs/*.md` were otherwise also copied into `guidelines/` (they already ship as `.prompt.md`).
- `[DTS_STYLE_SYSTEM]` filters React DOM props from `<Name>Props` (e.g. Button `disabled`/`onClick` are inherited from ButtonHTMLAttributes and do not appear in the emitted .d.ts).

## Fonts
- IBM Plex Sans KR ships ~95 unicode-range subsets × 4 weights → 394 woff2 files (~5 MB). Upload is chunked (≤50 files per call).

## Re-sync risks
- Toolchain floats on latest majors (TypeScript 7, Vite 8); a Vite asset-handling change could re-inline fonts into the CSS (the reason for the separate stylesheet build).
- `strip-woff.mjs` regex depends on fontsource's `url(...) format("woff")` output shape.

## Preview authoring notes (from verification waves)
- Meter: `.sg-meter__label` has no fixed width, so stacked meters with different label lengths start their tracks at different x. By design; an optional label-width prop would align stacks.
- Sparkline / StatusDot are tiny: previews lay them out in labeled flex rows with explicit sizes so they read at card scale.
- NodeRow is a `role="row"`; previews wrap it in `<div role="table">` (as NodeTable does) and need the same 1040x460 column override as NodeTable.
- ProcessTable: `.sg-proc` min-width 520px — previews use >=640px; the component has no empty state (rows=[] renders header only).
- Heatmap: column headers wrap (no `white-space: nowrap`); the preview uses a non-breaking hyphen in "sshd ‑t". Component polish candidate.
- CommandPalette inline with all groups is ~760px tall → `overrides.CommandPalette = {cardMode: column, viewport: 900x860}`.
- TabBar is `position: fixed` by default; `alwaysVisible` switches it to relative so it stays inside a card. ConfirmDialog `inline` needs no override.
- The 232px Sidebar rail wraps long labels+badges ("Persona & Debate" + "2 live"); keep badges short.

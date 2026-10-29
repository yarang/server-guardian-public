# Dashboard design language: warm paper light theme

English | [한국어](dashboard-notion-theme.ko.md)

Status: accepted (2026-10-19). Scope: `packages/guardian-ui` tokens and component styles, and through them
`apps/web-desktop`. Reference: [reference/DESIGN-notion.md](reference/DESIGN-notion.md), an external analysis of
Notion's marketing design language, kept verbatim (English only) as the source of the tokens below.

## 1. Decision

The dashboard moves from the dark graphite ops console to the reference's daylight language: a warm off-white
canvas, white cards defined by hairlines, near-black Inter type with tight headline tracking, and one structural
blue. The change is made in the design tokens (`src/styles/tokens.css`) and a small set of component rules; page
code in `apps/web-desktop` already reads every color from `--sg-*` tokens, so it needs no change.

## 2. Token mapping

| Reference | Token | Value |
|---|---|---|
| `canvas-soft` | `--sg-bg` | `#f6f5f4` |
| `surface` / `canvas` | `--sg-surface`, `--sg-surface-raised` | `#ffffff` |
| (sidebar, table heads) | `--sg-surface-sunken` | `#f1f0ee` |
| (code, terminal) | `--sg-code-bg` | `#f7f6f3` |
| `hairline` | `--sg-border` | `#e6e6e6` |
| text-input border | `--sg-border-strong` | `#dddddd` |
| `ink` at ~95% | `--sg-text` | `rgba(0,0,0,0.95)` |
| `ink-secondary` | `--sg-text-secondary` | `#31302e` |
| `ink-muted` | `--sg-text-muted` | `#615d59` |
| `primary` / `primary-active` | `--sg-accent` / `--sg-accent-hover` | `#0075de` / `#005bab` |
| radius `xs/sm/md/lg/xl/full` | `--sg-r-tag/sm/md/card(panel)/dialog/pill` | 4 / 5 / 8 / 12 / 16 / 9999 px |
| Level 1 / Level 2 shadow | `--sg-shadow-1` / `--sg-shadow-2` | layered stacks from the reference |
| headline tracking | `--sg-track-title/h3/h2/h1` | -0.125 / -0.25 / -0.625 / -1 px |

Components: buttons use weight 500 and 8px corners (the reference's utility button; dashboards have no marketing
CTA, so no pill buttons); the secondary button is white with a hairline. Text fields are white with 4px corners.
Status chips are eyebrow pills (sans 12px/600, no upper case). The active sidebar row carries a 2px blue
indicator. Dialogs and the command palette get the Level 2 shadow, the selected segment and hovered stat links
Level 1. The modal overlay is `--sg-overlay`.

## 3. Deviations from the reference

- **Semantic status colors.** The reference has no error/success ramp and keeps its sticker colors decorative. An
  ops console needs status, so `ok/warn/crit/info/idle` stay as a separate semantic ramp, darkened until each passes
  4.5:1 on white: ok `#16833a`, warn `#b25000`, crit `#d1242f`, info = accent, idle = muted. They appear only in
  dots, chips, meters, diff lines and warnings, never as a CTA fill except the danger button.
- **Sticker palette not used.** The dashboard has no illustrations; the accent colors are not tokens.
- **No dark hero band.** The indigo `secondary` "night" band is a marketing device; the dashboard has none.
- **Base size 14px.** The reference's dense body is 15px; dashboard tables and node rows keep 14px (and the 12/13px
  inline sizes in page code) to preserve density. Headline weights and tracking follow the reference.
- **Fonts.** `NotionInter` is proprietary; Inter (Latin subset, 400-700) is bundled from `@fontsource/inter`, with
  IBM Plex Sans KR as the Hangul fallback and JetBrains Mono for code.
- **Code and terminal are light.** `--sg-code-bg` is a warm paper tone so they read as document blocks; no dark
  islands remain.
- **No dark theme.** The previous graphite palette is in git history (`tokens.css` before this change). A theme
  switch in Settings would be a separate change (both message catalogs, `prefers-color-scheme`).

## 4. Rules for new UI

- Read colors only from `--sg-*` tokens; no hex values in page code.
- Blue (`--sg-accent`) is for the primary action, links, focus and selection; nothing decorative.
- Group with whitespace and hairlines before shadows; Level 2 shadow only for floating layers.
- Headings: weight 600-700 with the matching `--sg-track-*`; body stays at weight 400.

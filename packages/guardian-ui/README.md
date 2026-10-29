# @server-guardian/ui

English | [한국어](README.ko.md)

Warm paper light React components for the Server Guardian ops console (design language: [docs/design/dashboard-notion-theme.md](../../docs/design/dashboard-notion-theme.md)).

## Usage

```tsx
import '@server-guardian/ui/styles.css';
import { GuardianRoot, StatusChip } from '@server-guardian/ui';

<GuardianRoot>
  <StatusChip tone="ok">APPROVED</StatusChip>
</GuardianRoot>;
```

The JS entry never imports CSS. Import `@server-guardian/ui/styles.css` once; it contains tokens, component styles and `@font-face` rules whose woff2 files live in `dist/assets`.

The dashboard (`apps/web-desktop`) is a React app: its `src/main.tsx` imports the stylesheet and renders the whole app inside `GuardianRoot`. For vanilla pages that adopt components piece by piece, mount islands with `import { mount } from '@server-guardian/ui/mount'`; `mount(el, node)` returns an unmount function and does not add `GuardianRoot` for you.

The package also exports the shared types `Tone` (`ok`, `warn`, `crit`, `info`, `idle`), `Risk` (`LOW`, `MEDIUM`, `HIGH`) and the `RISK_TONE` map from risk to tone.

## UI messages

The built-in visible and accessible texts of the components (copy button, table headers, aria labels, risk and status words, dialog buttons, placeholders) are English by default (`DEFAULT_UI_MESSAGES`, type `UiMessages`). An application localizes the whole library once at startup, before rendering, with `setUiMessages(overrides)`; each section is merged over the English defaults, so a partial object is enough. `getUiMessages()` returns the active texts. A text passed as a prop to a component always wins over these defaults.

The dashboard fills them from its active language catalog: `apps/web-desktop/src/i18n/uiMessages.ts` calls `setUiMessages(tr.shell.ui)` and is imported by `src/main.tsx`, so the components show Korean or English together with the rest of the dashboard.

```tsx
import { setUiMessages } from '@server-guardian/ui';

setUiMessages({ copy: 'Copy', copied: 'Copied', confirmDialog: { cancel: 'Cancel' } });
```

## Scripts

- `npm run typecheck` / `npm test` / `npm run test:coverage`
- `npm run build` emits `dist/index.js`, `dist/mount.js`, the `.d.ts` tree, `dist/guardian-ui.css` and `dist/assets/*` (fonts).

Per-component docs are in `docs/`.

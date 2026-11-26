---
category: Foundations
---

# GuardianRoot

English | [한국어](GuardianRoot.ko.md)

Root wrapper that applies the Server Guardian font, text color, background and base size.

Import `@server-guardian/ui/styles.css` once at the app entry, then wrap the app (or every island mounted with `mount`) in `GuardianRoot`. Props: `theme` (only `light`, the warm paper theme and the default; set as `data-sg-theme`), plus any div attribute. The dashboard renders its whole app inside it in `apps/web-desktop/src/main.tsx`.

## Example

```tsx
<GuardianRoot>
  <Panel title="Fleet">site-b-ec1, site-a-arm1 and 9 more nodes</Panel>
</GuardianRoot>
```

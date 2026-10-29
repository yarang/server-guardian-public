---
category: Status
---

# StatusDot

English | [한국어](StatusDot.ko.md)

Small colored status indicator with an optional pulse and screen-reader label.

Props: `tone` (ok, warn, crit, info, idle; default idle), `size` in px (default 8), `pulse` (respects prefers-reduced-motion), `label` (sr-only text; default `idle`), plus any span attribute.

## Example 1

```tsx
<StatusDot tone="ok" pulse label="online" />
```

## Example 2

```tsx
<span><StatusDot tone="crit" label="offline" /> edge-mini</span>
```

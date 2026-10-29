---
category: Status
---

# StatCard

English | [한국어](StatCard.ko.md)

Compact KPI card with a label, large value, optional unit and tone-colored caption.

Props: `label` (default `Metric`), `value` (default `—`), `unit`, `caption`, `tone` (caption color), `href` (renders the card as a link), `className`.

## Example 1

```tsx
<StatCard label="Nodes online" value="11" unit="/ 12" caption="site-b-ec1 offline" tone="crit" />
```

## Example 2

```tsx
<StatCard label="Pending approvals" value="3" caption="Review now" tone="info" href="/approvals" />
```

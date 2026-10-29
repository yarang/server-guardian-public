---
category: Foundations
---

# Panel

English | [한국어](Panel.ko.md)

Titled container with header actions, body and footer on a 12px-radius surface.

Props: `title`, `actions` (header-right slot), `footer`, `tone` (emphasizes the full border; `idle` adds nothing), `flush` (no body padding, for tables), `className`. With a title the section is labelled by its heading.

## Example 1

```tsx
<Panel title="Pending approvals" actions={<Button size="sm" variant="outline">Refresh</Button>} footer="Last sync 12s ago">
  <StatCard label="Proposed" value="3" caption="1 HIGH risk" tone="crit" />
</Panel>
```

## Example 2

```tsx
<Panel title="SSH audit" tone="warn" flush>
  <Heatmap columns={['Root', 'Password']} rows={[{ name: 'site-b-ec1', cells: ['P', 'W'], score: 78 }]} />
</Panel>
```

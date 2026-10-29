---
category: Navigation
---

# NavItem

English | [한국어](NavItem.ko.md)

Single navigation link with optional icon and badge, highlighted when active.

Props: `label` (default `Link`), `href` (default `#`), `icon`, `badge` (a count such as pending approvals, or short text such as "Running"; `0` still renders, empty values hide it), `badgeTone` (`crit`, the default, for items that need attention; `info` for neutral activity), `active` (sets `aria-current="page"`). Defined in `Sidebar.tsx`, which also exports the `hasBadge` helper used by `Sidebar` and `TabBar`.

## Example 1

```tsx
<NavItem label="Approvals" href="/approvals" icon={<Icon name="shield" />} badge={3} />
```

## Example 2

```tsx
<NavItem label="Terminal" href="/terminal" icon={<Icon name="terminal" />} active />
```

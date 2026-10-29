---
category: Navigation
---

# Sidebar

English | [한국어](Sidebar.ko.md)

Left navigation rail with brand slot, nav items and footer; hidden below 760px (use TabBar there).

Props: `brand`, `items` (NavItem props), `sections` (headed groups `{ heading, items }`, rendered after `items`), `footer`, `children` (extra content inside the navigation, after the items and sections), `aria-label` (default the UI message `navPrimary`, English "Primary"; see the [README](../README.md)), `className`. Pair with `TabBar` for narrow screens.

## Example

```tsx
<Sidebar
  brand="Server Guardian"
  items={[
    { label: 'Fleet', href: '/fleet', icon: <Icon name="server" />, active: true },
    { label: 'Debates', href: '/debates', icon: <Icon name="chat" /> },
    { label: 'Approvals', href: '/approvals', icon: <Icon name="shield" />, badge: 3 },
  ]}
  footer="Hub: site-a-arm1"
/>
```

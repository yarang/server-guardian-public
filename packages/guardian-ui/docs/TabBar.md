---
category: Navigation
---

# TabBar

English | [한국어](TabBar.ko.md)

Bottom tab bar for narrow screens; hidden at 760px and wider unless alwaysVisible is set.

Props: `items` as `{ label, href?, icon?, active?, badge?, badgeTone? }` (`href` defaults to `#`; `badge` and `badgeTone` work as in `NavItem`), `alwaysVisible` (for previews), `aria-label` (default the UI message `navPrimary`, English "Primary"; see the [README](../README.md)), `className`. Fixed to the bottom of the viewport on narrow screens; in flow when `alwaysVisible`.

## Example

```tsx
<TabBar
  alwaysVisible
  items={[
    { label: 'Fleet', href: '/fleet', icon: <Icon name="server" />, active: true },
    { label: 'Debates', href: '/debates', icon: <Icon name="chat" /> },
    { label: 'Terminal', href: '/terminal', icon: <Icon name="terminal" /> },
  ]}
/>
```

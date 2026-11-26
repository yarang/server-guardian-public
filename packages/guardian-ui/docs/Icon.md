---
category: Foundations
---

# Icon

English | [한국어](Icon.ko.md)

Small inline stroke icon from a fixed set that inherits the current text color.

Names: search, shield, server, terminal, chat, bolt, check, x, chevron-right, chevron-left, trash, lock, plus, copy, grid, flow, gear (also exported as `ICON_NAMES`). `name` defaults to `check`. Decorative by default (`aria-hidden`); pass `title` to make it an accessible image (`role="img"` with `title` as its aria-label). `size` is in pixels (default 16). Any other SVG attribute is passed through.

## Example 1

```tsx
<Icon name="server" />
```

## Example 2

```tsx
<Icon name="lock" size={20} title="Locked by jev-mcp gate" />
```

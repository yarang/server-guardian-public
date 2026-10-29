---
category: Actions
---

# IconButton

English | [한국어](IconButton.ko.md)

Square icon-only button with a 40px touch target and a mandatory aria-label.

Props: `aria-label` (required), `icon` (default the plus icon), `variant` (primary, secondary, ghost, danger, outline; default `ghost`), plus any button attribute. Renders `<button type="button">` unless `type` is given.

## Example 1

```tsx
<IconButton aria-label="Copy command" icon={<Icon name="copy" />} />
```

## Example 2

```tsx
<IconButton aria-label="Remove node site-b-ec1" variant="danger" icon={<Icon name="trash" />} />
```

---
category: Actions
---

# Button

English | [한국어](Button.ko.md)

Primary action control available in primary, secondary, ghost, danger and outline variants.

Props: `variant` (default `secondary`), `size` (`sm` 32px, `md` 40px, `lg` 44px; default `md`), `icon` (leading element), `disabled`, `loading` (spinner in place of the icon, blocks clicks, sets `aria-busy`), `href` (renders an anchor styled as a button), `target` and `rel` (anchor only; `rel` defaults to `noopener noreferrer` when `target="_blank"`), plus any button attribute. Renders `<button type="button">` unless `type` is given. A disabled or loading anchor drops its `href` and sets `aria-disabled`.

## Example 1

```tsx
<Button variant="primary" icon={<Icon name="check" />} onClick={approve}>Approve</Button>
```

## Example 2

```tsx
<Button variant="danger" loading>Restarting sshd on site-b-ec1</Button>
<Button variant="outline" href="/debates/42">Open debate</Button>
```

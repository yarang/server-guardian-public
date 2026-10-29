---
category: Inputs
---

# TextField

English | [한국어](TextField.ko.md)

Labeled text input with optional hint, error message and monospace mode.

Props: `label` (real `<label>`; default `Field`), `value` or `defaultValue`, `onChange`, `placeholder`, `mono`, `hint`, `error` (sets `aria-invalid`, is announced with `role="alert"` and replaces the hint), `type` (default `text`), `id` (generated when omitted), plus any input attribute.

## Example 1

```tsx
<TextField label="Host" mono placeholder="site-b-ec1" hint="Hostname or IP of the node" />
```

## Example 2

```tsx
<TextField label="SSH port" type="number" defaultValue="2222" error="Port must be 1-65535" />
```

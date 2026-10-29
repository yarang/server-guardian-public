---
category: Inputs
---

# SegmentedControl

English | [한국어](SegmentedControl.ko.md)

Compact single-choice toggle group built from aria-pressed buttons.

Props: `options` as `{ value, label }`, `value` (controlled) or `defaultValue` (uncontrolled; falls back to the first option), `onChange(value)`, `aria-label` (default the UI message `segmentedOptions`, English "Options"; see the [README](../README.md)), `className`.

## Example

```tsx
<SegmentedControl
  aria-label="Time range"
  options={[{ value: '1h', label: '1h' }, { value: '24h', label: '24h' }, { value: '7d', label: '7d' }]}
  defaultValue="24h"
  onChange={setRange}
/>
```

---
category: Status
---

# Meter

English | [한국어](Meter.ko.md)

Horizontal usage meter that auto-colors green, amber or red from configurable thresholds.

Props: `value` (0-100, clamped; default 0), `label` (shown before the bar and used as the accessible name), `thresholds` (inclusive; default warn 70, crit 85, exported as `DEFAULT_THRESHOLDS`), `showValue` (default true), `size` (`sm`, `lg`; default `sm`), plus any div attribute. Exposes `role="meter"`; without a `label` the aria-label is the UI message `meterUsage` (English "Usage"; see the [README](../README.md)). `meterTone(value, thresholds)` is exported for custom use.

## Example 1

```tsx
<Meter value={42} label="CPU" />
```

## Example 2

```tsx
<Meter value={91} label="Disk /" size="lg" thresholds={{ warn: 75, crit: 90 }} />
```

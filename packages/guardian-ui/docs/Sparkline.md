---
category: Status
---

# Sparkline

English | [한국어](Sparkline.ko.md)

Tiny inline trend line for tables and cards; decorative (aria-hidden).

Props: `points` (oldest first; fewer than two points draw a flat line), `tone` (default `info`), `width` (default 96), `height` (default 28), `className`. Put the meaning in nearby text, since the SVG is hidden from assistive tech.

## Example 1

```tsx
<Sparkline points={[12, 18, 15, 30, 44, 38, 52]} tone="warn" />
```

## Example 2

```tsx
<span>CPU 52% <Sparkline points={[40, 42, 41, 45, 52]} tone="ok" width={64} height={20} /></span>
```

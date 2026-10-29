---
category: Status
---

# StatusChip

English | [한국어](StatusChip.ko.md)

Pill-shaped tinted chip for statuses such as APPROVED, DISPUTED, PENDING_JEV or HIGH.

Props: `tone` (default `idle`) and `children` (default `UNKNOWN`), plus any span attribute. Text uses the tone color, background ~12% alpha and border ~33% alpha.

## Example 1

```tsx
<StatusChip tone="ok">APPROVED</StatusChip>
```

## Example 2

```tsx
<StatusChip tone="warn">PENDING_JEV</StatusChip>
<StatusChip tone="crit">DISPUTED</StatusChip>
```

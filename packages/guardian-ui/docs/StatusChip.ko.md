---
category: Status
---

# StatusChip

[English](StatusChip.md) | 한국어

APPROVED, DISPUTED, PENDING_JEV, HIGH 같은 상태를 표시하는 알약 모양의 색조 칩이다.

Props: `tone`(기본값 `idle`)과 `children`(기본값 `UNKNOWN`), 그 밖의 모든 span 속성. 글자는 tone 색, 배경은 약 12% 알파, 테두리는 약 33% 알파를 쓴다.

## 예시 1

```tsx
<StatusChip tone="ok">APPROVED</StatusChip>
```

## 예시 2

```tsx
<StatusChip tone="warn">PENDING_JEV</StatusChip>
<StatusChip tone="crit">DISPUTED</StatusChip>
```

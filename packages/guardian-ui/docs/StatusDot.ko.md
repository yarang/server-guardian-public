---
category: Status
---

# StatusDot

[English](StatusDot.md) | 한국어

선택적인 맥동 효과와 스크린 리더용 레이블을 갖춘 작은 색 상태 표시기다.

Props: `tone`(ok, warn, crit, info, idle, 기본값 idle), `size`(px, 기본값 8), `pulse`(prefers-reduced-motion을 따름), `label`(sr-only 텍스트, 기본값 `idle`), 그 밖의 모든 span 속성.

## 예시 1

```tsx
<StatusDot tone="ok" pulse label="online" />
```

## 예시 2

```tsx
<span><StatusDot tone="crit" label="offline" /> edge-mini</span>
```

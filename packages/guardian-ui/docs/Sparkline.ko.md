---
category: Status
---

# Sparkline

[English](Sparkline.md) | 한국어

표와 카드에 넣는 작은 인라인 추세선이며, 장식용(aria-hidden)이다.

Props: `points`(오래된 값부터, 점이 둘 미만이면 평평한 선), `tone`(기본값 `info`), `width`(기본값 96), `height`(기본값 28), `className`. SVG는 보조 기술에서 숨겨지므로 의미는 주변 텍스트에 담는다.

## 예시 1

```tsx
<Sparkline points={[12, 18, 15, 30, 44, 38, 52]} tone="warn" />
```

## 예시 2

```tsx
<span>CPU 52% <Sparkline points={[40, 42, 41, 45, 52]} tone="ok" width={64} height={20} /></span>
```

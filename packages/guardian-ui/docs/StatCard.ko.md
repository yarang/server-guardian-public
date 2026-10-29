---
category: Status
---

# StatCard

[English](StatCard.md) | 한국어

레이블, 큰 값, 선택적인 단위, tone 색 캡션을 갖춘 작은 KPI 카드다.

Props: `label`(기본값 `Metric`), `value`(기본값 `—`), `unit`, `caption`, `tone`(캡션 색), `href`(카드 전체를 링크로 렌더링), `className`.

## 예시 1

```tsx
<StatCard label="Nodes online" value="11" unit="/ 12" caption="site-b-ec1 offline" tone="crit" />
```

## 예시 2

```tsx
<StatCard label="Pending approvals" value="3" caption="Review now" tone="info" href="/approvals" />
```

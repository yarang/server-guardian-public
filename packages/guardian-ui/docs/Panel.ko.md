---
category: Foundations
---

# Panel

[English](Panel.md) | 한국어

모서리 반경 12px 표면 위에 제목, 헤더 액션, 본문, 푸터를 갖춘 컨테이너다.

Props: `title`, `actions`(헤더 오른쪽 슬롯), `footer`, `tone`(테두리 전체를 강조, `idle`은 효과 없음), `flush`(본문 패딩 없음, 표용), `className`. 제목이 있으면 섹션의 레이블은 그 제목이 된다.

## 예시 1

```tsx
<Panel title="Pending approvals" actions={<Button size="sm" variant="outline">Refresh</Button>} footer="Last sync 12s ago">
  <StatCard label="Proposed" value="3" caption="1 HIGH risk" tone="crit" />
</Panel>
```

## 예시 2

```tsx
<Panel title="SSH audit" tone="warn" flush>
  <Heatmap columns={['Root', 'Password']} rows={[{ name: 'site-b-ec1', cells: ['P', 'W'], score: 78 }]} />
</Panel>
```

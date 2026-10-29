---
category: Navigation
---

# Sidebar

[English](Sidebar.md) | 한국어

브랜드 슬롯, 내비게이션 항목, 푸터를 갖춘 왼쪽 내비게이션 레일이다. 760px 미만에서는 숨겨진다(그때는 TabBar를 쓴다).

Props: `brand`, `items`(NavItem props), `sections`(제목이 있는 그룹 `{ heading, items }`, `items` 다음에 렌더링), `footer`, `children`(내비게이션 안에서 항목과 섹션 다음에 오는 추가 내용), `aria-label`(기본값은 UI 메시지 `navPrimary`, 영어 "Primary", [README](../README.ko.md) 참고), `className`. 좁은 화면용으로 `TabBar`와 함께 쓴다.

## 예시

```tsx
<Sidebar
  brand="Server Guardian"
  items={[
    { label: 'Fleet', href: '/fleet', icon: <Icon name="server" />, active: true },
    { label: 'Debates', href: '/debates', icon: <Icon name="chat" /> },
    { label: 'Approvals', href: '/approvals', icon: <Icon name="shield" />, badge: 3 },
  ]}
  footer="Hub: site-a-arm1"
/>
```

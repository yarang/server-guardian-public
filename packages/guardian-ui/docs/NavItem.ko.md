---
category: Navigation
---

# NavItem

[English](NavItem.md) | 한국어

선택적인 아이콘과 배지를 갖춘 단일 내비게이션 링크이며, 활성 상태일 때 강조된다.

Props: `label`(기본값 `Link`), `href`(기본값 `#`), `icon`, `badge`(대기 중인 승인 수 같은 숫자나 "Running" 같은 짧은 텍스트, `0`도 표시되며 빈 값이면 숨김), `badgeTone`(주의가 필요한 항목에는 기본값 `crit`, 중립적인 활동에는 `info`), `active`(`aria-current="page"` 설정). `Sidebar.tsx`에 정의되어 있으며, 이 파일은 `Sidebar`와 `TabBar`가 쓰는 `hasBadge` 헬퍼도 export한다.

## 예시 1

```tsx
<NavItem label="Approvals" href="/approvals" icon={<Icon name="shield" />} badge={3} />
```

## 예시 2

```tsx
<NavItem label="Terminal" href="/terminal" icon={<Icon name="terminal" />} active />
```

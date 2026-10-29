---
category: Navigation
---

# TabBar

[English](TabBar.md) | 한국어

좁은 화면용 하단 탭 바다. alwaysVisible을 설정하지 않으면 760px 이상에서는 숨겨진다.

Props: `items`(`{ label, href?, icon?, active?, badge?, badgeTone? }`, `href` 기본값은 `#`, `badge`와 `badgeTone`은 `NavItem`과 같이 동작), `alwaysVisible`(미리보기용), `aria-label`(기본값은 UI 메시지 `navPrimary`, 영어 "Primary", [README](../README.ko.md) 참고), `className`. 좁은 화면에서는 뷰포트 하단에 고정되고, `alwaysVisible`이면 일반 흐름에 놓인다.

## 예시

```tsx
<TabBar
  alwaysVisible
  items={[
    { label: 'Fleet', href: '/fleet', icon: <Icon name="server" />, active: true },
    { label: 'Debates', href: '/debates', icon: <Icon name="chat" /> },
    { label: 'Terminal', href: '/terminal', icon: <Icon name="terminal" /> },
  ]}
/>
```

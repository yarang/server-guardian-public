---
category: Foundations
---

# GuardianRoot

[English](GuardianRoot.md) | 한국어

Server Guardian의 글꼴, 글자색, 배경, 기본 크기를 적용하는 루트 래퍼다.

앱 엔트리에서 `@server-guardian/ui/styles.css`를 한 번 import한 뒤, 앱 전체(또는 `mount`로 마운트하는 각 아일랜드)를 `GuardianRoot`로 감싼다. Props: `theme`(`dark`만 있으며 기본값이고 `data-sg-theme`로 설정됨), 그 밖의 모든 div 속성. 대시보드는 `apps/web-desktop/src/main.tsx`에서 앱 전체를 이 컴포넌트 안에서 렌더링한다.

## 예시

```tsx
<GuardianRoot>
  <Panel title="Fleet">site-b-ec1, site-a-arm1 and 9 more nodes</Panel>
</GuardianRoot>
```

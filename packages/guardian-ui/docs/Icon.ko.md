---
category: Foundations
---

# Icon

[English](Icon.md) | 한국어

고정된 세트에서 고르는 작은 인라인 선 아이콘이며, 현재 글자색을 물려받는다.

이름: search, shield, server, terminal, chat, bolt, check, x, chevron-right, chevron-left, trash, lock, plus, copy, grid, flow, gear(`ICON_NAMES`로도 export됨). `name` 기본값은 `check`다. 기본적으로 장식용(`aria-hidden`)이며, `title`을 넘기면 접근 가능한 이미지(`role="img"`, `title`이 aria-label)가 된다. `size`는 픽셀 단위다(기본값 16). 그 밖의 SVG 속성은 그대로 전달된다.

## 예시 1

```tsx
<Icon name="server" />
```

## 예시 2

```tsx
<Icon name="lock" size={20} title="Locked by jev-mcp gate" />
```

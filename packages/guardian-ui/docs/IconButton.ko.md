---
category: Actions
---

# IconButton

[English](IconButton.md) | 한국어

40px 터치 영역과 필수 aria-label을 갖춘 정사각형 아이콘 전용 버튼이다.

Props: `aria-label`(필수), `icon`(기본값은 plus 아이콘), `variant`(primary, secondary, ghost, danger, outline, 기본값 `ghost`), 그 밖의 모든 button 속성. `type`을 주지 않으면 `<button type="button">`으로 렌더링한다.

## 예시 1

```tsx
<IconButton aria-label="Copy command" icon={<Icon name="copy" />} />
```

## 예시 2

```tsx
<IconButton aria-label="Remove node site-b-ec1" variant="danger" icon={<Icon name="trash" />} />
```

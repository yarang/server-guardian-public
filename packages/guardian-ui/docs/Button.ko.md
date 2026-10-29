---
category: Actions
---

# Button

[English](Button.md) | 한국어

primary, secondary, ghost, danger, outline 변형을 제공하는 기본 조작 버튼이다.

Props: `variant`(기본값 `secondary`), `size`(`sm` 32px, `md` 40px, `lg` 44px, 기본값 `md`), `icon`(앞쪽 요소), `disabled`, `loading`(아이콘 자리에 스피너를 표시하고 클릭을 막으며 `aria-busy`를 설정), `href`(버튼 모양의 앵커로 렌더링), `target`과 `rel`(앵커 전용, `target="_blank"`이면 `rel` 기본값은 `noopener noreferrer`), 그 밖의 모든 button 속성. `type`을 주지 않으면 `<button type="button">`으로 렌더링한다. disabled이거나 loading인 앵커는 `href`를 빼고 `aria-disabled`를 설정한다.

## 예시 1

```tsx
<Button variant="primary" icon={<Icon name="check" />} onClick={approve}>Approve</Button>
```

## 예시 2

```tsx
<Button variant="danger" loading>Restarting sshd on site-b-ec1</Button>
<Button variant="outline" href="/debates/42">Open debate</Button>
```

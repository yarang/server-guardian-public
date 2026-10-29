---
category: Inputs
---

# TextField

[English](TextField.md) | 한국어

선택적인 도움말, 오류 메시지, 고정폭 모드를 갖춘 레이블 달린 텍스트 입력창이다.

Props: `label`(실제 `<label>`, 기본값 `Field`), `value` 또는 `defaultValue`, `onChange`, `placeholder`, `mono`, `hint`, `error`(`aria-invalid`를 설정하고 `role="alert"`로 알리며 hint를 대신함), `type`(기본값 `text`), `id`(생략하면 생성됨), 그 밖의 모든 input 속성.

## 예시 1

```tsx
<TextField label="Host" mono placeholder="site-b-ec1" hint="Hostname or IP of the node" />
```

## 예시 2

```tsx
<TextField label="SSH port" type="number" defaultValue="2222" error="Port must be 1-65535" />
```

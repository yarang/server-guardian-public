---
category: Data
---

# CodeBlock

[English](CodeBlock.md) | 한국어

선택적인 diff 색 표시와 복사 버튼을 갖춘 고정폭 코드 뷰어다.

Props: `code`(줄바꿈으로 구분), `diff`(기본값 false, +로 시작하는 줄은 초록, -로 시작하는 줄은 빨강), `title`(헤더 바의 캡션), `copyable`(기본값 true), `className`. 헤더 바는 제목이나 복사 버튼이 있을 때 표시된다. 복사 버튼 레이블("Copy", 복사 후 1.5초 동안 "Copied")은 UI 메시지 `copy`와 `copied`에서 온다([README](../README.ko.md) 참고).

## 예시 1

```tsx
<CodeBlock title="sshd_config" code={"PermitRootLogin no\nPasswordAuthentication no"} />
```

## 예시 2

```tsx
<CodeBlock
  diff
  title="/etc/ssh/sshd_config"
  code={"-PasswordAuthentication yes\n+PasswordAuthentication no\n PubkeyAuthentication yes"}
/>
```

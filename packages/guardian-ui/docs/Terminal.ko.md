---
category: Data
---

# Terminal

[English](Terminal.md) | 한국어

호스트 탭, 스크롤되는 로그, 명령 입력창을 갖춘 터미널 세션 뷰다.

Props: `tabs`(비어 있으면 숨김), `activeTab`(controlled, 또는 0에서 시작하는 uncontrolled), `onTabChange(index)`, `lines`(`{ kind: 'prompt'|'out'|'err'|'meta', text }`), `prompt`(기본값 `$`), `placeholder`, `onRun(command)`, `className`. Enter를 누르면 앞뒤 공백을 제거한 명령으로 `onRun`을 호출하고 입력창을 비운다. 빈 명령은 무시한다. 세션·출력·입력 레이블과 기본 placeholder는 UI 메시지의 `terminal` 섹션에서 온다([README](../README.ko.md) 참고).

## 예시

```tsx
<Terminal
  tabs={['site-a-arm1', 'site-b-ec1']}
  prompt="guardian@site-a-arm1 $"
  lines={[
    { kind: 'prompt', text: 'systemctl status guardian-agent' },
    { kind: 'out', text: 'Active: active (running) since Mon 2026-09-28' },
    { kind: 'meta', text: 'exit 0' },
  ]}
  onRun={(cmd) => runOnHost('site-a-arm1', cmd)}
/>
```

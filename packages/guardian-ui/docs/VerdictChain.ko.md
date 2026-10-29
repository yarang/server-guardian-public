---
category: Status
---

# VerdictChain

[English](VerdictChain.md) | 한국어

토론 단계(전문가, 비판자, 판정자, 게이트)를 결과 색과 함께 보여 주는 세로 타임라인이다.

Props: `steps`(`{ label, result, tone?, detail? }`, tone 기본값은 `idle`, 위에서 아래로 렌더링), `className`. 목록의 aria-label은 UI 메시지 `verdictChain`(영어 "Verdict chain")이다([README](../README.ko.md) 참고).

## 예시

```tsx
<VerdictChain
  steps={[
    { label: 'Expert', result: 'PROPOSED', tone: 'info', detail: 'sshd 비밀번호 인증 비활성화 제안' },
    { label: 'Critic panel', result: 'DISPUTED', tone: 'warn', detail: '2 of 3 critics flagged lockout risk' },
    { label: 'Judge', result: 'APPROVED', tone: 'ok', detail: 'Approved with console-fallback patch' },
    { label: 'jev-mcp gate', result: 'PENDING_JEV', tone: 'warn' },
  ]}
/>
```

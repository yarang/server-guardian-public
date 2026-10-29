---
category: Data
---

# ProcessTable

[English](ProcessTable.md) | 한국어

PID, 사용자, CPU, 메모리, 명령 열을 갖춘 top 스타일의 프로세스 목록이다.

Props: `rows`(`{ pid, user, cpu, mem, command }`, `pid`는 숫자나 문자열, `cpu`와 `mem`은 소수점 한 자리로 표시되는 퍼센트), `className`. 긴 명령은 잘리고 전체 텍스트가 툴팁으로 표시된다. 좁을 때는 가로로 스크롤된다. User와 Command 헤더는 UI 메시지의 `processTable` 섹션에서 오며([README](../README.ko.md) 참고), PID, CPU %, MEM %는 그대로 표시된다.

## 예시

```tsx
<ProcessTable
  rows={[
    { pid: 1204, user: 'guardian', cpu: 12.4, mem: 3.1, command: '/usr/local/bin/guardian-agent --sse :9870' },
    { pid: 988, user: 'root', cpu: 0.3, mem: 0.9, command: '/usr/sbin/sshd -D' },
  ]}
/>
```

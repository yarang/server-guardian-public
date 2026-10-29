---
category: Data
---

# NodeTable

[English](NodeTable.md) | 한국어

열 헤더와 그룹별 NodeRow로 이루어진 플릿 표이며, 좁은 화면에서는 가로로 스크롤된다.

Props: `groups`(`{ name, summary?, nodes: NodeRowProps[] }`), `className`. `NodeRow.tsx`에 정의되어 있다. 표의 aria-label과 Node, Status, Disk, Load 헤더는 UI 메시지의 `nodeTable` 섹션에서 오며([README](../README.ko.md) 참고), CPU, MEM, SSH는 그대로 표시된다.

## 예시

```tsx
<NodeTable
  groups={[
    {
      name: 'Oracle Cloud',
      summary: '3 nodes · 1 hub',
      nodes: [
        { name: 'site-a-arm1', arch: 'arm64', tier: 'T1', isHub: true, status: 'online', cpu: 34, mem: 61, disk: 48, load: 0.42, sshScore: 94 },
        { name: 'site-b-ec1', arch: 'amd64', tier: 'T1', status: 'online', cpu: 78, mem: 72, disk: 91, load: 1.8, sshScore: 66 },
      ],
    },
    { name: 'Home lab', summary: '1 node', nodes: [{ name: 'edge-mini', tier: 'T2', status: 'offline' }] },
  ]}
/>
```

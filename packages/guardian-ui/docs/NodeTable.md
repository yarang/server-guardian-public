---
category: Data
---

# NodeTable

English | [한국어](NodeTable.ko.md)

Fleet table with a column header and grouped NodeRows that scrolls horizontally on narrow screens.

Props: `groups` as `{ name, summary?, nodes: NodeRowProps[] }`, `className`. Defined in `NodeRow.tsx`. The table's aria-label and the Node, Status, Disk and Load headers come from the UI messages `nodeTable` section (see the [README](../README.md)); CPU, MEM and SSH stay as they are.

## Example

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

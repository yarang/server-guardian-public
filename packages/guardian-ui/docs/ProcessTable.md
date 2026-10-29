---
category: Data
---

# ProcessTable

English | [한국어](ProcessTable.ko.md)

Top-style process list with PID, user, CPU, memory and command columns.

Props: `rows` as `{ pid, user, cpu, mem, command }` (`pid` a number or string; `cpu` and `mem` are percentages shown with one decimal), `className`. Long commands are truncated with the full text as a tooltip. Scrolls horizontally when narrow. The User and Command headers come from the UI messages `processTable` section (see the [README](../README.md)); PID, CPU % and MEM % stay as they are.

## Example

```tsx
<ProcessTable
  rows={[
    { pid: 1204, user: 'guardian', cpu: 12.4, mem: 3.1, command: '/usr/local/bin/guardian-agent --sse :9870' },
    { pid: 988, user: 'root', cpu: 0.3, mem: 0.9, command: '/usr/sbin/sshd -D' },
  ]}
/>
```

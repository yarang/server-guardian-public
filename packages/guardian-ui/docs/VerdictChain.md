---
category: Status
---

# VerdictChain

English | [한국어](VerdictChain.ko.md)

Vertical timeline of debate stages (Expert, Critic, Judge, gate) with colored results.

Props: `steps` as `{ label, result, tone?, detail? }` (tone defaults to `idle`), rendered top to bottom; `className`. The list's aria-label is the UI message `verdictChain` (English "Verdict chain"; see the [README](../README.md)).

## Example

```tsx
<VerdictChain
  steps={[
    { label: 'Expert', result: 'PROPOSED', tone: 'info', detail: 'Proposes disabling sshd password authentication' },
    { label: 'Critic panel', result: 'DISPUTED', tone: 'warn', detail: '2 of 3 critics flagged lockout risk' },
    { label: 'Judge', result: 'APPROVED', tone: 'ok', detail: 'Approved with console-fallback patch' },
    { label: 'jev-mcp gate', result: 'PENDING_JEV', tone: 'warn' },
  ]}
/>
```

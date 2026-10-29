---
category: Actions
---

# ActionCard

English | [한국어](ActionCard.ko.md)

Remediation action proposal card with risk and status chips, command preview and approve/reject buttons.

Props: `id` (default `act-0000`), `tool` (default `tool`), `risk` (LOW, MEDIUM, HIGH; default LOW), `status` (PROPOSED, HELD, RUNNING, EXECUTED, FAILED, REJECTED; default PROPOSED), `title`, `meta`, `command` (shown in a `CodeBlock` without a copy button), `warning` (a note callout), `onApprove`, `onReject`, `className`. Human approval is required, so Approve and Reject only appear for PROPOSED and HELD actions.

The risk chip text, the Approve and Reject labels, the fallback title ("Untitled action") and the card's aria-label ("Action <id>") come from the UI messages (`riskLabel`, `approve`, `reject`, `untitledAction`, `actionAria`; see the [README](../README.md)).

## Example 1

```tsx
<ActionCard
  id="act-7f3a"
  tool="ssh.harden"
  risk="HIGH"
  status="HELD"
  title="Disable PasswordAuthentication"
  meta="site-b-ec1 · proposed by Remediation agent"
  command="sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config"
  warning="jev-mcp gate held this action: no console fallback verified."
  onApprove={() => approve('act-7f3a')}
  onReject={() => reject('act-7f3a')}
/>
```

## Example 2

```tsx
<ActionCard id="act-81c2" tool="systemctl" risk="LOW" status="EXECUTED" title="Restart guardian-agent" meta="site-a-arm1" />
```

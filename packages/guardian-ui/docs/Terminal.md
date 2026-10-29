---
category: Data
---

# Terminal

English | [한국어](Terminal.ko.md)

Terminal session view with host tabs, a scrolling log and a command input.

Props: `tabs` (hidden when empty), `activeTab` (controlled, or uncontrolled starting at 0), `onTabChange(index)`, `lines` as `{ kind: 'prompt'|'out'|'err'|'meta', text }`, `prompt` (default `$`), `placeholder`, `onRun(command)`, `className`. Enter calls `onRun` with the trimmed command and clears the input; an empty command is ignored. The session, output and input labels and the default placeholder come from the UI messages `terminal` section (see the [README](../README.md)).

## Example

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

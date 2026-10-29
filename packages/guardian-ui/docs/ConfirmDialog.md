---
category: Overlays
---

# ConfirmDialog

English | [한국어](ConfirmDialog.ko.md)

Confirmation dialog for operator approvals; HIGH-risk actions require typing a phrase to enable confirm.

Props: `open` (default true), `title`, `description`, `risk` (default LOW), `confirmText` (default `CONFIRM`), `confirmLabel`, `children`, `onConfirm`, `onCancel`, `onReject` (adds a Reject button), `inline` (no overlay). With `risk="HIGH"` the confirm button is a danger button and stays disabled until `confirmText` is typed exactly; the typed text resets each time the dialog opens. Escape calls `onCancel`; modal mode also focuses the dialog and traps Tab.

The default title, the Confirm, Cancel and Reject labels, the risk chip text and the "Type <text> to confirm" label come from the UI messages (`confirmDialog`, `reject`, `riskLabel`; see the [README](../README.md)).

## Example 1

```tsx
<ConfirmDialog
  inline
  risk="HIGH"
  title="Disable password login on site-b-ec1"
  description="This changes sshd_config and restarts sshd. Existing sessions stay connected."
  confirmText="site-b-ec1"
  confirmLabel="Run action"
  onConfirm={run}
  onCancel={close}
  onReject={reject}
>
  <CodeBlock code="systemctl restart sshd" copyable={false} />
</ConfirmDialog>
```

## Example 2

```tsx
<ConfirmDialog inline risk="LOW" title="Restart guardian-agent" description="Brief metrics gap on site-a-arm1." onConfirm={run} onCancel={close} />
```

---
category: Data
---

# CodeBlock

English | [한국어](CodeBlock.ko.md)

Monospace code viewer with optional diff tinting and a copy button.

Props: `code` (newline-separated), `diff` (default false; lines starting with + are tinted green, - red), `title` (caption in the header bar), `copyable` (default true), `className`. The header bar is shown when there is a title or a copy button. The copy button label ("Copy", then "Copied" for 1.5 s) comes from the UI messages `copy` and `copied` (see the [README](../README.md)).

## Example 1

```tsx
<CodeBlock title="sshd_config" code={"PermitRootLogin no\nPasswordAuthentication no"} />
```

## Example 2

```tsx
<CodeBlock
  diff
  title="/etc/ssh/sshd_config"
  code={"-PasswordAuthentication yes\n+PasswordAuthentication no\n PubkeyAuthentication yes"}
/>
```

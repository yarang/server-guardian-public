---
category: Overlays
---

# CommandPalette

English | [한국어](CommandPalette.ko.md)

Searchable command palette dialog with scopes, grouped results and keyboard navigation.

Props: `open` (default true), `query`, `onQueryChange`, `scopes` (shown as a `SegmentedControl`), `activeScope`, `onScopeChange`, `groups` as `{ name, items }` with items `{ icon?, label, sub?, shortcut?, tag?, tagTone? }`, `selectedIndex` (across all groups), `onSelectedIndexChange`, `onSelect(item)`, `onClose`, `placeholder`, `inline` (no overlay, for previews). `query` and `selectedIndex` work controlled or uncontrolled. Filtering is the caller's job. Arrow keys move (wrapping around), Enter selects, Escape and a backdrop click call `onClose`. Pass `open={false}` to hide.

The dialog, search, scope and result labels, the "No results" text and the default placeholder come from the UI messages `commandPalette` section (see the [README](../README.md)).

## Example

```tsx
<CommandPalette
  inline
  query="oci"
  scopes={['All', 'Nodes', 'Actions']}
  activeScope="All"
  selectedIndex={0}
  groups={[
    { name: 'Nodes', items: [
      { icon: <Icon name="server" />, label: 'site-a-arm1', sub: 'Hub · arm64 · online', shortcut: 'G H' },
      { icon: <Icon name="server" />, label: 'site-b-ec1', sub: 'T1 · amd64 · disk 91%' },
    ] },
    { name: 'Actions', items: [
      { icon: <Icon name="bolt" />, label: 'Approve act-7f3a', sub: 'ssh.harden on site-b-ec1', tag: 'HELD', tagTone: 'warn' },
    ] },
  ]}
/>
```

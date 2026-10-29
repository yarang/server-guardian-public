---
category: Data
---

# Heatmap

English | [한국어](Heatmap.ko.md)

Check matrix (e.g. sshd audit per node) where each cell shows a glyph so state is never color-only.

## Props

| Prop | Type | Default | Notes |
| --- | --- | --- | --- |
| `columns` | `Array<string \| HeatmapColumn>` | `[]` | `HeatmapColumn` is `{ label, title?, key? }`. `title` becomes the header tooltip and its accessible description; `key` is the React key (defaults to `label`). Header cells never wrap; the table scrolls horizontally. |
| `rows` | `HeatmapRow[]` | `[]` | See below. |
| `rowLabel` | `string` | UI message `heatmap.rowLabel` (English `'Node'`) | Header of the first column. |
| `onSelectRow` | `(id: string) => void` | — | Makes each row header a real `<button type="button">` with `aria-pressed`. Without it nothing is interactive. |
| `selectedId` | `string` | — | Highlights the row with this id (tinted background and a thin inset ring). |
| `legend` | `boolean` | `false` | Shows a legend below the table listing only the cell states that appear. |
| `scoreLabel` | `string` | UI message `heatmap.scoreLabel` (English `'Score'`) | Header of the score column. |
| `showScore` | `boolean` | `true` | Hide the score column with `false`. |
| `className` | `string` | — | Extra class on the outer scroll wrapper. |

`HeatmapRow`: `{ name, id?, cells, score?, unavailable?, status? }`

- `id` is the React key and the value passed to `onSelectRow` / compared with `selectedId`; it defaults to `name`, so give ids when names can repeat.
- `unavailable` (a reason string) replaces the check cells with one cell spanning every check column that shows the reason; the score column shows "—".
- `status` (`{ tone, label }`) renders a small `StatusChip` next to the row name, e.g. `partial`.
- A row without `score` shows "—" in the score column.

Cell states (`HeatmapCell`):

| State | Glyph | Tone | Label |
| --- | --- | --- | --- |
| `P` | ✓ | ok | Pass |
| `W` | ! | warn | Warn |
| `F` | ✕ | crit | Fail |
| `I` | i | info | Info (informational, e.g. the default port 22) |
| `N` | – | idle, dashed | Not checked |

Each cell has an aria-label such as "Root login: Fail"; a cell without a column label uses "Check <n>". The labels above are the English defaults of the UI messages `heatmap.words`; the legend's aria-label and "Check <n>" also come from the `heatmap` section (see the [README](../README.md)).

## Examples

Fleet sshd audit with rule tooltips, an unreachable node and the legend:

```tsx
const columns = [
  { key: 'SEC-SSH-001', label: 'Root login', title: 'SEC-SSH-001: Disable Direct Root Login via SSH' },
  { key: 'SEC-SSH-002', label: 'Password auth', title: 'SEC-SSH-002: Enforce Key-Based Authentication Only' },
  { key: 'SEC-SSH-003', label: 'Pubkey auth', title: 'SEC-SSH-003: Enable Public Key Authentication' },
  { key: 'SEC-SSH-004', label: 'MaxAuthTries', title: 'SEC-SSH-004: Limit Maximum Authentication Attempts' },
  { key: 'SEC-SSH-005', label: 'Port', title: 'SEC-SSH-005: Custom Non-standard SSH Port' },
];

<Heatmap
  legend
  columns={columns}
  rows={[
    { id: 'a1', name: 'site-a-arm1', cells: ['P', 'P', 'P', 'P', 'I'], score: 95 },
    { id: 'b2', name: 'site-b-ec1', cells: ['F', 'F', 'P', 'N', 'P'], score: 40, status: { tone: 'warn', label: 'partial' } },
    { id: 'm1', name: 'edge-mini', cells: [], unavailable: 'unreachable: ssh: Could not resolve hostname edge-mini' },
  ]}
/>
```

Selectable rows that drive a detail view:

```tsx
const [selected, setSelected] = useState('b2');

<Heatmap columns={columns} rows={rows} selectedId={selected} onSelectRow={setSelected} />
```

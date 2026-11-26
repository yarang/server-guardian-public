import type { CSSProperties, ReactNode } from 'react';
import { tr } from '../i18n';

export interface Column<R> {
  header: string;
  render: (row: R) => ReactNode;
  mono?: boolean;
  align?: 'left' | 'right';
  /** Keep short values (times, kinds, states, addresses) on one line; the table scrolls instead. */
  nowrap?: boolean;
  /** Floor for a free-text column, so a narrow screen scrolls the table instead of breaking every letter. */
  minWidth?: number;
}

const cell: CSSProperties = {
  padding: 'var(--sg-s-3) var(--sg-s-4)',
  borderBottom: '1px solid var(--sg-border-subtle)',
  verticalAlign: 'top',
};

/** Plain token-styled table for data the design system has no dedicated table for. */
export function DataTable<R>({
  columns,
  rows,
  getKey,
  label,
  empty = tr.shell.dataTable.empty,
}: {
  columns: Column<R>[];
  rows: R[];
  getKey: (row: R, index: number) => string;
  label: string;
  empty?: string;
}) {
  if (rows.length === 0) return <div style={{ color: 'var(--sg-text-muted)' }}>{empty}</div>;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table aria-label={label} style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.header}
                scope="col"
                style={{
                  ...cell,
                  textAlign: c.align ?? 'left',
                  color: 'var(--sg-text-muted)',
                  fontSize: 12,
                  fontWeight: 600,
                  borderBottom: '1px solid var(--sg-border)',
                  whiteSpace: 'nowrap',
                }}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={getKey(r, i)}>
              {columns.map((c) => (
                <td
                  key={c.header}
                  style={{
                    ...cell,
                    textAlign: c.align ?? 'left',
                    fontFamily: c.mono ? 'var(--sg-font-mono)' : undefined,
                    minWidth: c.minWidth,
                    ...(c.nowrap ? { whiteSpace: 'nowrap' } : { overflowWrap: 'anywhere' }),
                  }}
                >
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

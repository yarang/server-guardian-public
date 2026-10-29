import type { ReactNode } from 'react';

export type NoticeTone = 'ok' | 'warn' | 'crit' | 'info';

/** Inline message box tinted by status tone. `crit` is announced as an alert, the rest as status. */
export function Notice({ tone, children }: { tone: NoticeTone; children: ReactNode }) {
  return (
    <div
      role={tone === 'crit' ? 'alert' : 'status'}
      style={{
        padding: 'var(--sg-s-4) var(--sg-s-5)',
        border: `1px solid var(--sg-${tone}-border)`,
        background: `var(--sg-${tone}-bg)`,
        borderRadius: 'var(--sg-r-md)',
        color: 'var(--sg-text)',
        fontSize: 13,
        lineHeight: 1.5,
        whiteSpace: 'pre-wrap',
        overflowWrap: 'anywhere',
      }}
    >
      {children}
    </div>
  );
}

/** Lines of hub warnings or errors, one per row. */
export function NoticeList({ tone, items, mark }: { tone: NoticeTone; items: string[]; mark?: string }) {
  if (items.length === 0) return null;
  return (
    <Notice tone={tone}>
      {items.map((t, i) => (
        <div key={i}>{`${mark ?? (tone === 'crit' ? '✗' : '⚠')} ${t}`}</div>
      ))}
    </Notice>
  );
}

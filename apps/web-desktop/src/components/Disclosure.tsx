import type { ReactNode } from 'react';

/**
 * Native details/summary disclosure with token styling. Pass `open` (and `onToggle` to follow the user's
 * own toggling) when the parent needs to open it, for example to reveal a field that failed validation.
 */
export function Disclosure({
  summary,
  children,
  open,
  onToggle,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
  onToggle?: (open: boolean) => void;
}) {
  return (
    <details open={open} onToggle={onToggle ? (e) => onToggle(e.currentTarget.open) : undefined} style={{ minWidth: 0 }}>
      <summary style={{ cursor: 'pointer', color: 'var(--sg-text-secondary)', fontSize: 13, overflowWrap: 'anywhere' }}>{summary}</summary>
      <div style={{ display: 'grid', gap: 'var(--sg-s-3)', marginTop: 'var(--sg-s-3)' }}>{children}</div>
    </details>
  );
}

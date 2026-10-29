import type { CSSProperties, ReactNode } from 'react';
import type { Tone } from '@server-guardian/ui';

/** Bordered token-styled box for list items; `tone` tints the whole border (no side stripes). */
export function Card({ tone, children, style }: { tone?: Tone; children?: ReactNode; style?: CSSProperties }) {
  return (
    <div
      style={{
        display: 'grid',
        gap: 'var(--sg-s-3)',
        minWidth: 0,
        padding: 'var(--sg-s-6)',
        background: 'var(--sg-surface-raised)',
        border: `1px solid ${tone && tone !== 'idle' ? `var(--sg-${tone}-border)` : 'var(--sg-border)'}`,
        borderRadius: 'var(--sg-r-card)',
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/** Small muted caption used above a value block. */
export function Caption({ children }: { children: ReactNode }) {
  return <div style={{ color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600 }}>{children}</div>;
}

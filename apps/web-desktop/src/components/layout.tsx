import type { CSSProperties, ReactNode } from 'react';

/** Vertical stack with design-system spacing. */
export function Stack({ children, gap = 'var(--sg-s-10)', style }: { children?: ReactNode; gap?: string; style?: CSSProperties }) {
  return <div style={{ display: 'grid', gap, minWidth: 0, ...style }}>{children}</div>;
}

/** Responsive grid whose columns are at least `min` wide. */
export function Grid({ children, min = 220, gap = 'var(--sg-s-8)' }: { children?: ReactNode; min?: number; gap?: string }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fit, minmax(${min}px, 1fr))`, gap, minWidth: 0 }}>
      {children}
    </div>
  );
}

/** Horizontal wrapping row. */
export function Row({ children, gap = 'var(--sg-s-4)', style }: { children?: ReactNode; gap?: string; style?: CSSProperties }) {
  return <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap, ...style }}>{children}</div>;
}

export const mono: CSSProperties = { fontFamily: 'var(--sg-font-mono)' };
export const muted: CSSProperties = { color: 'var(--sg-text-muted)' };

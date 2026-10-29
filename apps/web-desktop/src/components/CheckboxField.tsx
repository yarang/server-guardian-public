import type { InputHTMLAttributes, ReactNode } from 'react';

/** Native checkbox with a clickable label, aligned to the first text line. */
export function CheckboxField({
  children,
  ...rest
}: { children: ReactNode } & Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'children'>) {
  return (
    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--sg-s-3)', cursor: 'pointer', minWidth: 0 }}>
      <input type="checkbox" {...rest} style={{ marginTop: 3, accentColor: 'var(--sg-accent)' }} />
      <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{children}</span>
    </label>
  );
}

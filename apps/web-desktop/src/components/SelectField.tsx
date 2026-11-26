import { useId } from 'react';
import type { SelectHTMLAttributes } from 'react';

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

/** Labeled native select styled with design tokens (the design system has no select yet). */
export function SelectField({
  label,
  options,
  ...rest
}: { label: string; options: SelectOption[] } & Omit<SelectHTMLAttributes<HTMLSelectElement>, 'children'>) {
  const id = useId();
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)', minWidth: 'min(220px, 100%)', maxWidth: '100%' }}>
      <label htmlFor={id} style={{ color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600 }}>
        {label}
      </label>
      <select
        id={id}
        {...rest}
        style={{
          // A long option text must not widen the field past its container (narrow screens).
          minWidth: 0,
          maxWidth: '100%',
          height: 40,
          padding: '0 var(--sg-s-4)',
          background: 'var(--sg-surface)',
          color: 'var(--sg-text)',
          border: '1px solid var(--sg-border-strong)',
          borderRadius: 'var(--sg-r-tag)',
          fontFamily: 'var(--sg-font-mono)',
          fontSize: 13,
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

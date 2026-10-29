import { useId } from 'react';
import type { TextareaHTMLAttributes } from 'react';

/** Labeled textarea styled like the design system's TextField (the design system has no textarea yet). */
export function TextAreaField({
  label,
  mono = false,
  hint,
  error,
  rows = 4,
  ...rest
}: { label: string; mono?: boolean; hint?: string; error?: string } & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const id = useId();
  const descId = error || hint ? `${id}-desc` : undefined;
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)', minWidth: 0 }}>
      <label htmlFor={id} style={{ color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600 }}>
        {label}
      </label>
      <textarea
        spellCheck={false}
        {...rest}
        id={id}
        rows={rows}
        aria-invalid={error ? true : undefined}
        aria-describedby={descId}
        style={{
          padding: 'var(--sg-s-4) var(--sg-s-5)',
          background: 'var(--sg-surface)',
          color: 'var(--sg-text)',
          border: `1px solid ${error ? 'var(--sg-crit)' : 'var(--sg-border-strong)'}`,
          borderRadius: 'var(--sg-r-tag)',
          fontFamily: mono ? 'var(--sg-font-mono)' : 'var(--sg-font-sans)',
          fontSize: mono ? 13 : 'var(--sg-font-size)',
          lineHeight: 1.5,
          resize: 'vertical',
          width: '100%',
          boxSizing: 'border-box',
        }}
      />
      {error ? (
        <span id={descId} role="alert" style={{ color: 'var(--sg-crit)', fontSize: 12 }}>
          {error}
        </span>
      ) : hint ? (
        <span id={descId} style={{ color: 'var(--sg-text-muted)', fontSize: 12 }}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

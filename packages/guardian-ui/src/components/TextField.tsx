import { useId } from 'react';
import type { InputHTMLAttributes } from 'react';
import { cx } from '../utils/cx';

/** Props for {@link TextField}. */
export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  /** Visible label rendered as a real `<label>`. */
  label?: string;
  /** Use the monospace font (hosts, commands, tokens). */
  mono?: boolean;
  /** Helper text under the input. */
  hint?: string;
  /** Error message; marks the input invalid. */
  error?: string;
}

/**
 * Labeled text input with optional hint, error message and monospace mode.
 */
export function TextField({
  label = 'Field',
  mono = false,
  hint,
  error,
  id,
  className,
  type = 'text',
  ...rest
}: TextFieldProps) {
  const auto = useId();
  const inputId = id ?? auto;
  const descId = error || hint ? `${inputId}-desc` : undefined;
  return (
    <div className={cx('sg-field', className)}>
      <label className="sg-field__label" htmlFor={inputId}>
        {label}
      </label>
      <input
        {...rest}
        id={inputId}
        type={type}
        className={cx('sg-field__input', mono && 'sg-field__input--mono')}
        aria-invalid={error ? true : undefined}
        aria-describedby={descId}
      />
      {error ? (
        <span className="sg-field__error" id={descId} role="alert">
          {error}
        </span>
      ) : hint ? (
        <span className="sg-field__hint" id={descId}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

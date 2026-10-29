import type { HTMLAttributes, ReactNode } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';

/** Props for {@link StatusChip}. */
export interface StatusChipProps extends HTMLAttributes<HTMLSpanElement> {
  /** Status color (tinted background, border and text). */
  tone?: Tone;
  /** Chip label, usually an uppercase status such as APPROVED or PENDING_JEV. */
  children?: ReactNode;
}

/**
 * Pill-shaped tinted chip for statuses such as APPROVED, DISPUTED, PENDING_JEV or HIGH.
 */
export function StatusChip({
  tone = 'idle',
  children = 'UNKNOWN',
  className,
  ...rest
}: StatusChipProps) {
  return (
    <span className={cx('sg-chip', `sg-chip--${tone}`, className)} {...rest}>
      {children}
    </span>
  );
}

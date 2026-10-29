import type { HTMLAttributes } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';

/** Props for {@link StatusDot}. */
export interface StatusDotProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'children'> {
  /** Status color. */
  tone?: Tone;
  /** Diameter in pixels. */
  size?: number;
  /** Animate a soft pulse ring (disabled under prefers-reduced-motion). */
  pulse?: boolean;
  /** Screen-reader text describing the status; the dot itself is purely visual. */
  label?: string;
}

/**
 * Small colored status indicator with an optional pulse and screen-reader label.
 */
export function StatusDot({
  tone = 'idle',
  size = 8,
  pulse = false,
  label = 'idle',
  className,
  style,
  ...rest
}: StatusDotProps) {
  return (
    <span
      className={cx('sg-dot', `sg-dot--${tone}`, pulse && 'sg-dot--pulse', className)}
      style={{ width: size, height: size, ...style }}
      {...rest}
    >
      <span className="sg-sr-only">{label}</span>
    </span>
  );
}

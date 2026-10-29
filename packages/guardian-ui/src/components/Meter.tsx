import type { HTMLAttributes } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';

/** Warn and crit thresholds (inclusive) used to color a {@link Meter}. */
export interface MeterThresholds {
  warn: number;
  crit: number;
}

/** Props for {@link Meter}. */
export interface MeterProps extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  /** Value from 0 to 100; out-of-range values are clamped. */
  value?: number;
  /** Optional text shown before the bar (also the accessible name). */
  label?: string;
  /** Thresholds that switch the bar to warn / crit color. Defaults to warn 70, crit 85. */
  thresholds?: MeterThresholds;
  /** Show the numeric value after the bar. */
  showValue?: boolean;
  /** Bar thickness. */
  size?: 'sm' | 'lg';
}

/** Default Meter thresholds. */
export const DEFAULT_THRESHOLDS: MeterThresholds = { warn: 70, crit: 85 };

/** Resolve the tone for a meter value (crit >= crit threshold, warn >= warn threshold, else ok). */
export function meterTone(value: number, thresholds: MeterThresholds = DEFAULT_THRESHOLDS): Tone {
  if (value >= thresholds.crit) return 'crit';
  if (value >= thresholds.warn) return 'warn';
  return 'ok';
}

/**
 * Horizontal usage meter that auto-colors green, amber or red from configurable thresholds.
 */
export function Meter({
  value = 0,
  label,
  thresholds = DEFAULT_THRESHOLDS,
  showValue = true,
  size = 'sm',
  className,
  ...rest
}: MeterProps) {
  const safe = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
  const tone = meterTone(safe, thresholds);
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(safe)}
      aria-label={label ?? getUiMessages().meterUsage}
      data-tone={tone}
      className={cx('sg-meter', size === 'lg' && 'sg-meter--lg', className)}
      {...rest}
    >
      {label ? <span className="sg-meter__label">{label}</span> : null}
      <span className="sg-meter__track">
        <span className={cx('sg-meter__fill', `sg-meter__fill--${tone}`)} style={{ width: `${safe}%` }} />
      </span>
      {showValue ? <span className="sg-meter__value">{Math.round(safe)}%</span> : null}
    </div>
  );
}

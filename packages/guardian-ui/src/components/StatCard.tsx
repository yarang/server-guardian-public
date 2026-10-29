import type { ReactNode } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';

/** Props for {@link StatCard}. */
export interface StatCardProps {
  /** Metric name. */
  label?: string;
  /** Main figure. */
  value?: ReactNode;
  /** Unit rendered smaller next to the value. */
  unit?: string;
  /** Supporting line under the value. */
  caption?: ReactNode;
  /** Color of the caption text. */
  tone?: Tone;
  /** When set the whole card renders as a link. */
  href?: string;
  className?: string;
}

/**
 * Compact KPI card with a label, large value, optional unit and tone-colored caption.
 */
export function StatCard({
  label = 'Metric',
  value = '—',
  unit,
  caption,
  tone,
  href,
  className,
}: StatCardProps) {
  const body = (
    <>
      <span className="sg-stat__label">{label}</span>
      <span className="sg-stat__value">
        {value}
        {unit ? <span className="sg-stat__unit">{unit}</span> : null}
      </span>
      {caption ? (
        <span className={cx('sg-stat__caption', tone && `sg-stat__caption--${tone}`)}>{caption}</span>
      ) : null}
    </>
  );
  const cls = cx('sg-stat', className);
  return href ? (
    <a className={cls} href={href}>
      {body}
    </a>
  ) : (
    <div className={cls}>{body}</div>
  );
}

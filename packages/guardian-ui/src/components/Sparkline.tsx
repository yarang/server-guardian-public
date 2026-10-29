import type { Tone } from '../types';
import { cx } from '../utils/cx';

/** Props for {@link Sparkline}. */
export interface SparklineProps {
  /** Data points, oldest first. Fewer than two points renders a flat line. */
  points?: number[];
  /** Stroke color. */
  tone?: Tone;
  width?: number;
  height?: number;
  className?: string;
}

/**
 * Tiny inline trend line for tables and cards; decorative (aria-hidden).
 */
export function Sparkline({ points = [], tone = 'info', width = 96, height = 28, className }: SparklineProps) {
  const pad = 2;
  const w = Math.max(width - pad * 2, 1);
  const h = Math.max(height - pad * 2, 1);
  let coords: Array<[number, number]>;
  if (points.length < 2) {
    coords = [
      [pad, height / 2],
      [width - pad, height / 2],
    ];
  } else {
    const min = Math.min(...points);
    const max = Math.max(...points);
    const span = max - min || 1;
    coords = points.map((p, i) => [
      pad + (i / (points.length - 1)) * w,
      pad + h - ((p - min) / span) * h,
    ]);
  }
  const d = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  return (
    <svg
      className={cx('sg-spark', `sg-spark--${tone}`, className)}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
      focusable="false"
    >
      <polyline className="sg-spark__line" points={d} />
    </svg>
  );
}

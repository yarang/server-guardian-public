import type { ReactNode } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { StatusChip } from './StatusChip';

/** One step in a {@link VerdictChain}. */
export interface VerdictStep {
  /** Stage name, e.g. Expert, Critic panel, Judge, jev-mcp gate. */
  label: string;
  /** Outcome text shown as a chip, e.g. APPROVED. */
  result: string;
  /** Color of the marker and chip. */
  tone?: Tone;
  /** Longer explanation under the label. */
  detail?: ReactNode;
}

/** Props for {@link VerdictChain}. */
export interface VerdictChainProps {
  /** Ordered steps, rendered top to bottom. */
  steps?: VerdictStep[];
  className?: string;
}

/**
 * Vertical timeline of debate stages (Expert, Critic, Judge, gate) with colored results.
 */
export function VerdictChain({ steps = [], className }: VerdictChainProps) {
  return (
    <ol className={cx('sg-chain', className)} aria-label={getUiMessages().verdictChain}>
      {steps.map((s, i) => {
        const tone = s.tone ?? 'idle';
        return (
          <li className="sg-chain__step" key={`${s.label}-${i}`}>
            <div className="sg-chain__rail" aria-hidden="true">
              <span className={cx('sg-chain__marker', `sg-chain__marker--${tone}`)} />
              <span className="sg-chain__line" />
            </div>
            <div>
              <div className="sg-chain__head">
                <span className="sg-chain__label">{s.label}</span>
                <StatusChip tone={tone}>{s.result}</StatusChip>
              </div>
              {s.detail ? <p className="sg-chain__detail">{s.detail}</p> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

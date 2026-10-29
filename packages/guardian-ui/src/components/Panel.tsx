import { useId } from 'react';
import type { ReactNode } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';

/** Props for {@link Panel}. */
export interface PanelProps {
  /** Header title. */
  title?: ReactNode;
  /** Header-right slot, e.g. buttons or a SegmentedControl. */
  actions?: ReactNode;
  /** Footer slot. */
  footer?: ReactNode;
  /** Emphasizes the whole border with a status tone (no side stripes). */
  tone?: Tone;
  /** Remove body padding, e.g. for tables. */
  flush?: boolean;
  className?: string;
  children?: ReactNode;
}

/**
 * Titled container with header actions, body and footer on a 12px-radius surface.
 */
export function Panel({
  title,
  actions,
  footer,
  tone,
  flush = false,
  className,
  children,
}: PanelProps) {
  const titleId = useId();
  return (
    <section
      className={cx('sg-panel', tone && tone !== 'idle' && `sg-panel--${tone}`, className)}
      aria-labelledby={title ? titleId : undefined}
    >
      {title || actions ? (
        <header className="sg-panel__header">
          {title ? (
            <h3 className="sg-panel__title" id={titleId}>
              {title}
            </h3>
          ) : (
            <span />
          )}
          {actions ? <div className="sg-panel__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="sg-panel__body" style={flush ? { padding: 0 } : undefined}>
        {children}
      </div>
      {footer ? <footer className="sg-panel__footer">{footer}</footer> : null}
    </section>
  );
}

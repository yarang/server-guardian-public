import type { HTMLAttributes } from 'react';
import { cx } from '../utils/cx';

/** Props for {@link GuardianRoot}. */
export interface GuardianRootProps extends HTMLAttributes<HTMLDivElement> {
  /** Color theme. Only the dark graphite theme exists today. */
  theme?: 'dark';
}

/**
 * Root wrapper that applies the Server Guardian font, text color, background and base size.
 *
 * Wrap your whole app (or each mounted island) in this component; import
 * `@server-guardian/ui/styles.css` once at the application entry.
 */
export function GuardianRoot({ theme = 'dark', className, children, ...rest }: GuardianRootProps) {
  return (
    <div className={cx('sg-root', className)} data-sg-theme={theme} {...rest}>
      {children}
    </div>
  );
}

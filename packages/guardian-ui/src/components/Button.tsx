import type { AnchorHTMLAttributes, ButtonHTMLAttributes, MouseEvent, ReactNode } from 'react';
import { cx } from '../utils/cx';

/** Props for {@link Button}. */
export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  /** Visual style. */
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline';
  /** Control height. `md` is 40px, `lg` is 44px. */
  size?: 'sm' | 'md' | 'lg';
  /** Optional leading icon element. */
  icon?: ReactNode;
  /** Shows a spinner and blocks interaction. */
  loading?: boolean;
  /** When set, renders an anchor styled as a button. */
  href?: string;
  /** Anchor target (only used with `href`). */
  target?: string;
  /** Anchor rel (only used with `href`). */
  rel?: string;
  children?: ReactNode;
}

/**
 * Primary action control available in primary, secondary, ghost, danger and outline variants.
 *
 * Renders a real `<button>`, or an `<a>` when `href` is provided.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  loading = false,
  disabled = false,
  href,
  target,
  rel,
  className,
  children = 'Button',
  type = 'button',
  onClick,
  ...rest
}: ButtonProps) {
  const cls = cx('sg-btn', `sg-btn--${variant}`, size !== 'md' && `sg-btn--${size}`, className);
  const inner = (
    <>
      {loading ? <span className="sg-btn__spinner" aria-hidden="true" /> : icon}
      {children}
    </>
  );
  const blocked = disabled || loading;

  if (href !== undefined) {
    const handle = (e: MouseEvent<HTMLAnchorElement>) => {
      if (blocked) {
        e.preventDefault();
        return;
      }
      onClick?.(e as unknown as MouseEvent<HTMLButtonElement>);
    };
    return (
      <a
        {...(rest as unknown as AnchorHTMLAttributes<HTMLAnchorElement>)}
        className={cls}
        href={blocked ? undefined : href}
        target={target}
        rel={rel ?? (target === '_blank' ? 'noopener noreferrer' : undefined)}
        aria-disabled={blocked || undefined}
        aria-busy={loading || undefined}
        onClick={handle}
      >
        {inner}
      </a>
    );
  }

  return (
    <button
      {...rest}
      type={type}
      className={cls}
      disabled={blocked}
      aria-busy={loading || undefined}
      onClick={onClick}
    >
      {inner}
    </button>
  );
}

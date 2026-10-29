import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cx } from '../utils/cx';
import { Icon } from './Icon';

/** Props for {@link IconButton}. */
export interface IconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label' | 'children'> {
  /** Required accessible name, since the button has no visible text. */
  'aria-label': string;
  /** Icon element; defaults to the plus icon. */
  icon?: ReactNode;
  /** Visual style. */
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline';
}

/**
 * Square icon-only button with a 40px touch target and a mandatory aria-label.
 */
export function IconButton({
  icon = <Icon name="plus" />,
  variant = 'ghost',
  className,
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      className={cx('sg-iconbtn', variant !== 'ghost' && `sg-iconbtn--${variant}`, className)}
    >
      {icon}
    </button>
  );
}

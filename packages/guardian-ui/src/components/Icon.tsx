import type { ReactNode, SVGAttributes } from 'react';
import { cx } from '../utils/cx';

/** Names available in the built-in stroke icon set. */
export type IconName =
  | 'search'
  | 'shield'
  | 'server'
  | 'terminal'
  | 'chat'
  | 'bolt'
  | 'check'
  | 'x'
  | 'chevron-right'
  | 'chevron-left'
  | 'trash'
  | 'lock'
  | 'plus'
  | 'copy';

/** All icon names, useful for galleries and tests. */
export const ICON_NAMES: IconName[] = [
  'search',
  'shield',
  'server',
  'terminal',
  'chat',
  'bolt',
  'check',
  'x',
  'chevron-right',
  'chevron-left',
  'trash',
  'lock',
  'plus',
  'copy',
];

const PATHS: Record<IconName, ReactNode> = {
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </>
  ),
  shield: <path d="M12 3 4 6v6c0 4.5 3.2 8 8 9 4.8-1 8-4.5 8-9V6z" />,
  server: (
    <>
      <rect x="3" y="4" width="18" height="6" rx="1.5" />
      <rect x="3" y="14" width="18" height="6" rx="1.5" />
      <path d="M7 7h.01M7 17h.01" />
    </>
  ),
  terminal: (
    <>
      <path d="m4 17 6-5-6-5" />
      <path d="M12 19h8" />
    </>
  ),
  chat: <path d="M21 12a8 8 0 0 1-11.7 7L4 20l1.1-4.3A8 8 0 1 1 21 12z" />,
  bolt: <path d="M13 2 4 14h7l-1 8 9-12h-7z" />,
  check: <path d="m5 12 5 5 9-10" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  'chevron-right': <path d="m9 6 6 6-6 6" />,
  'chevron-left': <path d="m15 6-6 6 6 6" />,
  trash: <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />,
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9" rx="2" />
      <path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  copy: (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V6a2 2 0 0 1 2-2h9" />
    </>
  ),
};

/** Props for {@link Icon}. */
export interface IconProps extends Omit<SVGAttributes<SVGSVGElement>, 'name'> {
  /** Icon to draw. */
  name?: IconName;
  /** Pixel size (width and height). */
  size?: number;
  /** Accessible title; when omitted the icon is hidden from assistive tech. */
  title?: string;
}

/**
 * Small inline stroke icon that inherits the current text color.
 *
 * Decorative by default (aria-hidden); pass `title` to expose it to screen readers.
 */
export function Icon({ name = 'check', size = 16, title, className, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cx('sg-icon', className)}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      focusable="false"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}

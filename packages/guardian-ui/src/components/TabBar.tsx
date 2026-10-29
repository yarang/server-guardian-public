import type { ReactNode } from 'react';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { hasBadge } from './Sidebar';

/** One entry of a {@link TabBar}. */
export interface TabBarItem {
  label: string;
  href?: string;
  /** Icon element shown above the label. */
  icon?: ReactNode;
  active?: boolean;
  /** Optional badge: a count or short text shown next to the label. */
  badge?: ReactNode;
  /** Badge colour: `crit` (default) or `info`. */
  badgeTone?: 'crit' | 'info';
}

/** Props for {@link TabBar}. */
export interface TabBarProps {
  /** Tab entries. */
  items?: TabBarItem[];
  /** Show at every width instead of only under 760px (useful for previews). */
  alwaysVisible?: boolean;
  'aria-label'?: string;
  className?: string;
}

/**
 * Bottom tab bar for narrow screens; hidden at 760px and wider unless `alwaysVisible` is set.
 */
export function TabBar({
  items = [],
  alwaysVisible = false,
  className,
  'aria-label': ariaLabel = getUiMessages().navPrimary,
}: TabBarProps) {
  return (
    <nav className={cx('sg-tabbar', alwaysVisible && 'sg-tabbar--always', className)} aria-label={ariaLabel}>
      {items.map((it, i) => (
        <a
          key={`${it.label}-${i}`}
          className={cx('sg-tab', it.active && 'sg-tab--active')}
          href={it.href ?? '#'}
          aria-current={it.active ? 'page' : undefined}
        >
          {it.icon}
          <span>{it.label}</span>
          {hasBadge(it.badge) ? (
            <span className={cx('sg-nav__badge', 'sg-tab__badge', it.badgeTone === 'info' && 'sg-nav__badge--info')}>{it.badge}</span>
          ) : null}
        </a>
      ))}
    </nav>
  );
}

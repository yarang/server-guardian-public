import type { ReactNode } from 'react';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';

/** Props for {@link NavItem}. */
export interface NavItemProps {
  /** Link text. */
  label?: string;
  /** Destination URL. */
  href?: string;
  /** Optional leading icon element. */
  icon?: ReactNode;
  /** Optional badge: a count (e.g. pending approvals) or short text (e.g. "Running"). */
  badge?: ReactNode;
  /** Badge colour: `crit` (default, needs attention) or `info` (neutral activity). */
  badgeTone?: 'crit' | 'info';
  /** Marks the current page (sets aria-current). */
  active?: boolean;
}

/** False for the "no badge" values (undefined, null, false, empty string); 0 still renders. */
export function hasBadge(badge: ReactNode): boolean {
  return badge !== undefined && badge !== null && badge !== false && badge !== '';
}

/**
 * Single navigation link with optional icon and badge, highlighted when active.
 */
export function NavItem({ label = 'Link', href = '#', icon, badge, badgeTone = 'crit', active = false }: NavItemProps) {
  return (
    <a
      className={cx('sg-nav', active && 'sg-nav--active')}
      href={href}
      aria-current={active ? 'page' : undefined}
    >
      {icon}
      <span className="sg-nav__label">{label}</span>
      {hasBadge(badge) ? <span className={cx('sg-nav__badge', badgeTone === 'info' && 'sg-nav__badge--info')}>{badge}</span> : null}
    </a>
  );
}

/** A headed group of navigation entries for {@link Sidebar}. */
export interface SidebarSection {
  /** Group heading shown above the items. */
  heading: string;
  items: NavItemProps[];
}

/** Props for {@link Sidebar}. */
export interface SidebarProps {
  /** Brand slot at the top (logo and product name). */
  brand?: ReactNode;
  /** Navigation entries. */
  items?: NavItemProps[];
  /** Headed groups of entries, rendered after `items`. */
  sections?: SidebarSection[];
  /** Footer slot, e.g. hub status or user menu. */
  footer?: ReactNode;
  /** Accessible name of the navigation landmark. */
  'aria-label'?: string;
  className?: string;
  /** Extra content between the nav list and the footer. */
  children?: ReactNode;
}

/**
 * Left navigation rail with brand slot, nav items and footer; hidden below 760px (use TabBar there).
 */
export function Sidebar({
  brand,
  items = [],
  sections = [],
  footer,
  children,
  className,
  'aria-label': ariaLabel = getUiMessages().navPrimary,
}: SidebarProps) {
  return (
    <aside className={cx('sg-sidebar', className)}>
      {brand ? <div className="sg-sidebar__brand">{brand}</div> : null}
      <nav className="sg-sidebar__nav" aria-label={ariaLabel}>
        {items.map((it, i) => (
          <NavItem key={`${it.label ?? 'item'}-${i}`} {...it} />
        ))}
        {sections.map((section) => (
          <div key={section.heading} className="sg-sidebar__section" role="group" aria-label={section.heading}>
            <div className="sg-sidebar__heading" aria-hidden="true">
              {section.heading}
            </div>
            {section.items.map((it, i) => (
              <NavItem key={`${it.label ?? 'item'}-${i}`} {...it} />
            ))}
          </div>
        ))}
        {children}
      </nav>
      {footer ? <div className="sg-sidebar__footer">{footer}</div> : null}
    </aside>
  );
}

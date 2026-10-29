import { useEffect, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { Panel } from '@server-guardian/ui';

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/**
 * Modal overlay around a Panel. The design system has no generic dialog (ConfirmDialog is risk-specific),
 * so this composes one: role=dialog, Escape closes when `onClose` is given, Tab stays inside.
 */
export function Modal({
  title,
  onClose,
  width = 440,
  children,
}: {
  title: string;
  onClose?: () => void;
  /** Maximum dialog width in px. */
  width?: number;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>('input, button, select');
    (first ?? ref.current)?.focus();
    return () => previous?.focus?.();
  }, []);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    // A dialog opened from inside another one (React events bubble through the component tree) owns Escape and Tab.
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose?.();
      return;
    }
    if (e.key === 'Tab' && ref.current) {
      e.stopPropagation();
      const nodes = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 100,
        display: 'grid',
        placeItems: 'center',
        padding: 'var(--sg-s-8)',
        background: 'color-mix(in srgb, var(--sg-bg) 78%, transparent)',
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={onKey}
        style={{
          width: `min(${width}px, 100%)`,
          maxHeight: 'calc(100vh - 2 * var(--sg-s-8))',
          overflowY: 'auto',
          borderRadius: 'var(--sg-r-panel)',
          outline: 'none',
        }}
      >
        <Panel title={title}>{children}</Panel>
      </div>
    </div>
  );
}

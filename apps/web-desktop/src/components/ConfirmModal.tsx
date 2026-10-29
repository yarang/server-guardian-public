import type { ReactNode } from 'react';
import { Button } from '@server-guardian/ui';
import { tr } from '../i18n';
import { Modal } from './Modal';
import { Notice } from './Notice';

/**
 * Generic confirm step (delete, reject, publish, close). The design system's ConfirmDialog is risk-specific, so
 * this composes one from Modal. An `error` from the failed confirm is shown inside the dialog.
 */
export function ConfirmModal({
  title,
  message,
  confirmLabel,
  cancelLabel = tr.shell.confirm.cancel,
  tone = 'danger',
  confirmDisabled = false,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel: string;
  /** Label of the dismiss button. */
  cancelLabel?: string;
  /** Colour of the confirm button: `danger` for destructive steps (default), `primary` for reversible ones. */
  tone?: 'danger' | 'primary';
  /** Keeps Confirm disabled while the input is not valid yet. */
  confirmDisabled?: boolean;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={busy ? undefined : onCancel}>
      <div style={{ display: 'grid', gap: 'var(--sg-s-6)' }}>
        <div style={{ color: 'var(--sg-text-secondary)', lineHeight: 1.5, overflowWrap: 'anywhere' }}>{message}</div>
        {error ? <Notice tone="crit">{error}</Notice> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sg-s-3)', flexWrap: 'wrap' }}>
          <Button variant="ghost" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant={tone} loading={busy} disabled={confirmDisabled} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

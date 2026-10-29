import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { RISK_TONE } from '../types';
import type { Risk } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { Button } from './Button';
import { StatusChip } from './StatusChip';

/** Props for {@link ConfirmDialog}. */
export interface ConfirmDialogProps {
  /** Show the dialog. Defaults to true so it renders when used with no props. */
  open?: boolean;
  title?: string;
  description?: ReactNode;
  /** Risk of the action. HIGH requires typing `confirmText` before confirming. */
  risk?: Risk;
  /** Text the operator must type when risk is HIGH. */
  confirmText?: string;
  /** Label of the confirm button. */
  confirmLabel?: string;
  /** Extra content, e.g. a CodeBlock with the command. */
  children?: ReactNode;
  onConfirm?: () => void;
  onCancel?: () => void;
  /** When provided, a Reject button is shown. */
  onReject?: () => void;
  /** Render in flow without the fixed overlay (for previews). */
  inline?: boolean;
}

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/**
 * Confirmation dialog for operator approvals; HIGH-risk actions require typing a phrase to enable confirm.
 */
export function ConfirmDialog({
  open = true,
  title,
  description,
  risk = 'LOW',
  confirmText = 'CONFIRM',
  confirmLabel,
  children,
  onConfirm,
  onCancel,
  onReject,
  inline = false,
}: ConfirmDialogProps) {
  const uid = useId();
  const ui = getUiMessages();
  const dlg = ui.confirmDialog;
  const [typed, setTyped] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  const needsTyping = risk === 'HIGH';
  const matches = !needsTyping || typed === confirmText;

  // Reset the typed phrase whenever the dialog is reopened.
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  // Move focus into the modal dialog when it opens.
  useEffect(() => {
    if (open && !inline) ref.current?.focus();
  }, [open, inline]);

  if (!open) return null;

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      onCancel?.();
      return;
    }
    if (e.key === 'Tab' && !inline && ref.current) {
      const nodes = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === ref.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };

  const dialog = (
    <div
      ref={ref}
      className={cx('sg-dialog', needsTyping && 'sg-dialog--high')}
      role="alertdialog"
      aria-modal={inline ? undefined : true}
      aria-labelledby={`${uid}-title`}
      aria-describedby={description ? `${uid}-desc` : undefined}
      tabIndex={-1}
      onKeyDown={onKey}
    >
      <div className="sg-dialog__head">
        <h2 className="sg-dialog__title" id={`${uid}-title`}>
          {title ?? dlg.title}
        </h2>
        <StatusChip tone={RISK_TONE[risk]}>{ui.riskLabel(risk)}</StatusChip>
      </div>
      {description ? (
        <p className="sg-dialog__desc" id={`${uid}-desc`}>
          {description}
        </p>
      ) : null}
      {children}
      {needsTyping ? (
        <div className="sg-field">
          <label className="sg-field__label" htmlFor={`${uid}-confirm`}>
            {dlg.typeToConfirm(confirmText)}
          </label>
          <input
            id={`${uid}-confirm`}
            className="sg-field__input sg-field__input--mono"
            type="text"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
      ) : null}
      <div className="sg-dialog__actions">
        <Button variant="ghost" onClick={onCancel}>
          {dlg.cancel}
        </Button>
        {onReject ? (
          <Button variant="outline" onClick={onReject}>
            {ui.reject}
          </Button>
        ) : null}
        <Button variant={needsTyping ? 'danger' : 'primary'} disabled={!matches} onClick={onConfirm}>
          {confirmLabel ?? dlg.confirm}
        </Button>
      </div>
    </div>
  );

  if (inline) return dialog;
  return <div className="sg-overlay sg-overlay--center">{dialog}</div>;
}

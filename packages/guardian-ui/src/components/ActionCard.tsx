import type { ReactNode } from 'react';
import { RISK_TONE } from '../types';
import type { Risk, Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { Button } from './Button';
import { CodeBlock } from './CodeBlock';
import { Icon } from './Icon';
import { StatusChip } from './StatusChip';

/** Lifecycle status of a remediation action. */
export type ActionStatus = 'PROPOSED' | 'HELD' | 'RUNNING' | 'EXECUTED' | 'FAILED' | 'REJECTED';

const STATUS_TONE: Record<ActionStatus, Tone> = {
  PROPOSED: 'info',
  HELD: 'warn',
  RUNNING: 'info',
  EXECUTED: 'ok',
  FAILED: 'crit',
  REJECTED: 'idle',
};

/** Props for {@link ActionCard}. */
export interface ActionCardProps {
  /** Action identifier, e.g. act-7f3a. */
  id?: string;
  /** Tool that will run, e.g. ssh.harden or systemctl. */
  tool?: string;
  /** Risk classification. */
  risk?: Risk;
  /** Lifecycle status. Only PROPOSED and HELD actions show approval buttons. */
  status?: ActionStatus;
  /** Human-readable summary. */
  title?: ReactNode;
  /** Secondary line, e.g. target host and proposer. */
  meta?: ReactNode;
  /** Exact command to be executed. */
  command?: string;
  /** Called when the operator approves. */
  onApprove?: () => void;
  /** Called when the operator rejects. */
  onReject?: () => void;
  /** Warning callout, e.g. why the jev gate held the action. */
  warning?: ReactNode;
  className?: string;
}

/**
 * Remediation action proposal card with risk and status chips, command preview and approve/reject buttons.
 *
 * Human approval is required: buttons appear for PROPOSED and HELD actions only.
 */
export function ActionCard({
  id = 'act-0000',
  tool = 'tool',
  risk = 'LOW',
  status = 'PROPOSED',
  title,
  meta,
  command,
  onApprove,
  onReject,
  warning,
  className,
}: ActionCardProps) {
  const ui = getUiMessages();
  const actionable = status === 'PROPOSED' || status === 'HELD';
  return (
    <article className={cx('sg-action', className)} aria-label={ui.actionAria(id)}>
      <div className="sg-action__top">
        <span className="sg-action__id">{id}</span>
        <span className="sg-tag">{tool}</span>
        <span className="sg-action__spacer" />
        <StatusChip tone={RISK_TONE[risk]}>{ui.riskLabel(risk)}</StatusChip>
        <StatusChip tone={STATUS_TONE[status]}>{status}</StatusChip>
      </div>
      <h4 className="sg-action__title">{title ?? ui.untitledAction}</h4>
      {meta ? <p className="sg-action__meta">{meta}</p> : null}
      {command ? (
        <CodeBlock code={command} copyable={false} />
      ) : null}
      {warning ? (
        <div className="sg-action__warning" role="note">
          <Icon name="shield" />
          <span>{warning}</span>
        </div>
      ) : null}
      {actionable ? (
        <div className="sg-action__buttons">
          <Button variant="primary" icon={<Icon name="check" />} onClick={onApprove}>
            {ui.approve}
          </Button>
          <Button variant="outline" icon={<Icon name="x" />} onClick={onReject}>
            {ui.reject}
          </Button>
        </div>
      ) : null}
    </article>
  );
}

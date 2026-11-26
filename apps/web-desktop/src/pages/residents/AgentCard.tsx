import { StatusChip } from '@server-guardian/ui';
import { Ago } from '../../components/Ago';
import { Row, mono, muted } from '../../components/layout';
import { parseTime } from '../../lib/debate';
import { hrefFor } from '../../router';
import type { ResidentAgent } from '../../types';
import { tr } from '../../i18n';
import { agentStateLabel, agentStateTone, elapsedSince } from './format';

const cardStyle = {
  display: 'grid',
  gap: 'var(--sg-s-3)',
  padding: 'var(--sg-s-5)',
  background: 'var(--sg-surface-raised)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  textDecoration: 'none',
  color: 'var(--sg-text)',
  minWidth: 0,
} as const;

/** Small mono meta line of a card (provider, driver, epoch, ...). */
const meta = { ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' } as const;

/**
 * One resident agent as a clickable card: address, state, tools, node / provider / driver, epoch,
 * bind failures, how long it has been not ready, its open turn, open mail and held tasks. The link
 * opens the agent's detail (`?agent=<address>`); nothing on the card writes.
 */
export function AgentCard({ agent }: { agent: ResidentAgent }) {
  const notReadySince = parseTime(agent.not_ready_since);
  const open = agent.turn;
  return (
    <a
      href={hrefFor('debates', ['residents'], { agent: agent.address })}
      aria-label={tr.residents.openAgent(agent.address)}
      style={cardStyle}
    >
      <Row>
        <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{agent.address}</strong>
        <StatusChip tone={agentStateTone(agent.state)} title={agent.state ?? undefined}>
          {agentStateLabel(agent.state)}
        </StatusChip>
        {agent.tools ? (
          <StatusChip tone={agent.tools === 'open' ? 'warn' : 'idle'} title={agent.tools}>
            {agent.tools === 'open' ? tr.residents.toolsOpen : tr.residents.toolsClosed}
          </StatusChip>
        ) : null}
        {agent.in_store ? null : <StatusChip tone="warn">{tr.residents.declaredOnly}</StatusChip>}
        {agent.declared ? null : <StatusChip tone="warn">{tr.residents.notDeclared}</StatusChip>}
      </Row>
      <Row gap="var(--sg-s-3)">
        {agent.node ? <span style={meta}>{agent.node}</span> : null}
        {agent.provider ? <span style={meta}>{agent.provider}</span> : null}
        {agent.driver ? <span style={meta}>{tr.residents.driverLabel(agent.driver)}</span> : null}
      </Row>
      <Row gap="var(--sg-s-3)">
        {agent.epoch !== null ? <span style={meta}>{tr.residents.epoch(agent.epoch)}</span> : null}
        {agent.bind_failures ? (
          <span style={meta} title={agent.bind_started ?? undefined}>
            {tr.residents.bindFailures(agent.bind_failures)}
          </span>
        ) : null}
        {agent.state !== null && agent.state !== 'ready' && notReadySince !== null ? (
          <span style={{ ...muted, fontSize: 12 }}>
            <Ago at={notReadySince} prefix={tr.residents.notReadyPrefix} />
          </span>
        ) : null}
      </Row>
      {open ? (
        <Row gap="var(--sg-s-3)">
          <StatusChip tone="info">{tr.common.running}</StatusChip>
          {open.kind ? <span style={meta}>{open.kind}</span> : null}
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.turnElapsed(elapsedSince(open.started, Date.now()))}</span>
        </Row>
      ) : null}
      <Row gap="var(--sg-s-3)">
        <span style={{ ...muted, fontSize: 12 }}>
          {tr.residents.mailOpen(agent.mail_open.queued ?? 0, agent.mail_open.delivered ?? 0, agent.mail_open.retrieved ?? 0)}
        </span>
        {agent.tasks_held !== null ? (
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.tasksHeld(agent.tasks_held)}</span>
        ) : null}
      </Row>
    </a>
  );
}

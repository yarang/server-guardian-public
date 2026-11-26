import { Panel, StatusChip } from '@server-guardian/ui';
import { Ago } from '../../components/Ago';
import { Card } from '../../components/Card';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Row, Stack, mono, muted } from '../../components/layout';
import { parseTime } from '../../lib/debate';
import { hrefFor } from '../../router';
import type { ResidentAgent, ResidentMail, ResidentQuery, ResidentScopedSession, ResidentTask, ResidentTurn } from '../../types';
import { tr } from '../../i18n';
import { agentStateLabel, agentStateTone, turnDuration } from './format';
import { AgentMail } from './AgentMail';
import { SessionQueryPanel } from './SessionQueryPanel';
import { TaskCard } from './TaskBoard';

const meta = { ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' } as const;

/**
 * One resident agent (`?agent=<address>`): its recent turns, the mail it sent or received (a body
 * loads on demand), and the tasks it holds or is assigned. Everything here is read-only.
 */
export function ResidentAgentDetail({
  agent,
  turns,
  mail,
  tasks,
  queries = [],
  sessions = [],
  onRefresh,
}: {
  agent: ResidentAgent;
  turns: ResidentTurn[];
  mail: ResidentMail[];
  tasks: ResidentTask[];
  queries?: ResidentQuery[];
  sessions?: ResidentScopedSession[];
  onRefresh?: () => void;
}) {
  const agentMail = mail.filter((m) => m.sender === agent.address || m.recipient === agent.address);
  const agentTasks = tasks.filter((t) => t.assignee === agent.address || t.owner === agent.address);

  const turnColumns: Column<ResidentTurn>[] = [
    {
      header: tr.residents.colStarted,
      nowrap: true,
      render: (t) => {
        const at = t.started ? parseTime(t.started) : null;
        return at !== null ? <Ago at={at} /> : <span style={muted}>{t.started ?? tr.common.dash}</span>;
      },
    },
    { header: tr.residents.colKind, nowrap: true, mono: true, render: (t) => t.kind ?? tr.common.dash },
    {
      header: tr.residents.colDuration,
      nowrap: true,
      render: (t) => (t.ended ? turnDuration(t.started, t.ended) : tr.common.running),
    },
    { header: tr.residents.colEndReason, nowrap: true, mono: true, render: (t) => t.end_reason ?? tr.common.dash },
    { header: tr.residents.colHop, render: (t) => (t.hop_in !== null ? String(t.hop_in) : tr.common.dash) },
    { header: tr.residents.colInputs, render: (t) => (t.inputs !== null ? String(t.inputs) : tr.common.dash) },
  ];

  return (
    <Stack gap="var(--sg-s-6)">
      <div>
        <a href={hrefFor('debates', ['residents'])}>{tr.residents.back}</a>
      </div>
      <Card>
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
        </Row>
        <Row gap="var(--sg-s-3)">
          {agent.node ? <span style={meta}>{agent.node}</span> : null}
          {agent.provider ? <span style={meta}>{agent.provider}</span> : null}
          {agent.driver ? <span style={meta}>{tr.residents.driverLabel(agent.driver)}</span> : null}
          {agent.epoch !== null ? <span style={meta}>{tr.residents.epoch(agent.epoch)}</span> : null}
        </Row>
      </Card>
      <Panel title={tr.residents.queryTitle}>
        <SessionQueryPanel agent={agent} queries={queries} sessions={sessions} onSubmitted={onRefresh} />
      </Panel>
      <Panel title={tr.residents.turnsTitle}>
        <DataTable label={tr.residents.turnsTitle} columns={turnColumns} rows={turns} getKey={(t, i) => t.id ?? String(i)} empty={tr.residents.noTurns} />
      </Panel>
      <Panel title={tr.residents.mailTitle}>
        <AgentMail mail={agentMail} />
      </Panel>
      <Panel title={tr.residents.tasksTitle}>
        {agentTasks.length === 0 ? (
          <div style={{ ...muted, fontSize: 13 }}>{tr.residents.noTasks}</div>
        ) : (
          <Stack gap="var(--sg-s-4)">
            {agentTasks.map((task) => (
              <TaskCard key={task.id} task={task} showStatus />
            ))}
          </Stack>
        )}
      </Panel>
    </Stack>
  );
}

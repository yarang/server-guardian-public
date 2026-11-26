import { Button, Panel } from '@server-guardian/ui';
import { fetchResidentMonitor } from '../../api';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice, NoticeList } from '../../components/Notice';
import { Grid, Row, Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { fmtDateTime } from '../../lib/format';
import type { ResidentMonitor } from '../../types';
import { tr } from '../../i18n';
import { AgentCard } from './AgentCard';
import { EventsFeed } from './EventsFeed';
import { ResidentAgentDetail } from './ResidentAgentDetail';
import { TaskBoard } from './TaskBoard';

/** The monitor polls this often while the tab is visible; a hidden tab pauses (one fetch on return). */
export const RESIDENT_POLL_MS = 10_000;

/**
 * The resident agents tab (`#/debates/residents`, docs/design/resident-monitor.md RM3): one
 * read-only snapshot of the mail store, polled every 10 s. Agents are hub-wide, so the selected
 * project changes nothing here; `?agent=<address>` opens one agent's detail. The only control on
 * this tab is the refresh button.
 */
export function ResidentsTab({ query }: { query: Record<string, string> }) {
  const state = useHubData(fetchResidentMonitor, { pollMs: RESIDENT_POLL_MS, pauseWhenHidden: true });
  return (
    <DataGate state={state} what={tr.residents.what}>
      {(snap) => <MonitorView snap={snap} selected={query.agent ?? ''} loading={state.loading} onRefresh={state.reload} />}
    </DataGate>
  );
}

function MonitorView({
  snap,
  selected,
  loading,
  onRefresh,
}: {
  snap: ResidentMonitor;
  selected: string;
  loading: boolean;
  onRefresh: () => void;
}) {
  const agent = selected ? (snap.agents.find((a) => a.address === selected) ?? null) : null;
  const addresses = snap.agents.map((a) => a.address);
  return (
    <Stack>
      <Card>
        <Row style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div style={{ display: 'grid', gap: 'var(--sg-s-2)', minWidth: 0 }}>
            <span>{tr.residents.snapshotAt(snap.generated ? fmtDateTime(snap.generated) : tr.common.dash)}</span>
            <span style={{ ...muted, fontSize: 12 }}>{tr.residents.hubWide}</span>
          </div>
          <Button size="sm" variant="outline" loading={loading} onClick={onRefresh}>
            {tr.common.refresh}
          </Button>
        </Row>
      </Card>
      {/* Problems are the monitor's own read findings (a missing table, a schema mismatch, ...). */}
      <NoticeList tone="warn" items={snap.problems} />
      {snap.store.exists === false ? <Notice tone="info">{tr.residents.noStore}</Notice> : null}
      {selected && !agent ? <Notice tone="warn">{tr.residents.agentMissing(selected)}</Notice> : null}
      {agent ? (
        <ResidentAgentDetail
          agent={agent}
          turns={snap.turns[agent.address] ?? []}
          mail={snap.mail}
          tasks={snap.tasks}
          queries={snap.queries ?? []}
          sessions={snap.sessions ?? []}
          onRefresh={onRefresh}
        />
      ) : snap.agents.length === 0 ? (
        <Card style={{ textAlign: 'center', ...muted }}>{tr.residents.noAgents}</Card>
      ) : (
        <Grid min={280} gap="var(--sg-s-5)">
          {snap.agents.map((a) => (
            <AgentCard key={a.address} agent={a} />
          ))}
        </Grid>
      )}
      <Panel title={tr.residents.boardTitle}>
        <TaskBoard tasks={snap.tasks} />
      </Panel>
      <Panel title={tr.residents.artifactsTitle}>
        {snap.artifacts && snap.artifacts.length > 0 ? (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: 'left', borderBottom: '1px solid var(--sg-border-subtle)' }}>
                  <th style={{ padding: '6px 8px' }}>{tr.residents.colCreated}</th>
                  <th style={{ padding: '6px 8px' }}>{tr.residents.colAgent}</th>
                  <th style={{ padding: '6px 8px' }}>{tr.residents.colArtifactTitle}</th>
                  <th style={{ padding: '6px 8px' }}>{tr.residents.colArtifactFile}</th>
                  <th style={{ padding: '6px 8px' }}>{tr.residents.colBytes}</th>
                  <th style={{ padding: '6px 8px' }} />
                </tr>
              </thead>
              <tbody>
                {snap.artifacts.map((art) => (
                  <tr key={art.id} style={{ borderBottom: '1px solid var(--sg-border-subtle)' }}>
                    <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>{fmtDateTime(art.created)}</td>
                    <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>{art.agent}</td>
                    <td style={{ padding: '6px 8px' }}>{art.title}</td>
                    <td style={{ padding: '6px 8px', fontFamily: 'var(--sg-font-mono)' }}>{art.filename}</td>
                    <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>{art.size_bytes} B</td>
                    <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>
                      <a href={`/api/residents/artifacts/${encodeURIComponent(art.id)}?download=1`} download={art.filename}>
                        {tr.residents.downloadArtifact}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ ...muted, fontSize: 13 }}>{tr.residents.noArtifacts}</div>
        )}
      </Panel>
      <Panel title={tr.residents.eventsTitle}>
        <EventsFeed events={snap.events} agents={addresses} />
      </Panel>
    </Stack>
  );
}

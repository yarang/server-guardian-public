import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { tr } from '../../i18n';
import { fmtDateTime } from '../../lib/format';
import type { WorkerReport, WorkerRow } from '../../types';

const STATE_TONE = { up: 'ok', down: 'crit', off: 'idle' } as const;

function Fact({ label, children }: { label: string; children: string }) {
  return (
    <div>
      <span style={muted}>{`${label}: `}</span>
      <span style={mono}>{children}</span>
    </div>
  );
}

function WorkerCard({ w, codeVersion }: { w: WorkerRow; codeVersion: string }) {
  const t = tr.debate.workers;
  return (
    <Card>
      <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
        <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{w.id}</strong>
        <StatusChip tone={STATE_TONE[w.state]}>{t.state[w.state]}</StatusChip>
      </Row>
      <div style={{ ...muted, fontSize: 12 }}>
        {w.capable ? t.capable : t.notCapable}
        {w.checked ? ` | ${t.checked(fmtDateTime(w.checked))}` : ''}
        {w.max_jobs ? ` | ${t.maxJobs(w.max_jobs)}` : ''}
      </div>
      {w.state === 'up' ? (
        <Stack gap="var(--sg-s-1)">
          <Fact label={t.version}>{w.version ?? '?'}</Fact>
          {w.current === false ? <Notice tone="warn">{t.stale(codeVersion)}</Notice> : null}
          <Fact label={t.slots}>{`${w.free_slots ?? '?'} / ${w.max_jobs ?? '?'}`}</Fact>
          <Fact label={t.providers}>{(w.providers ?? []).join(', ') || tr.common.none}</Fact>
          <Fact label={t.git}>{w.git ? tr.common.yes : tr.common.no}</Fact>
        </Stack>
      ) : null}
      {w.state === 'down' && w.error ? <Notice tone="crit">{w.error}</Notice> : null}
      {w.state === 'off' ? <div style={{ ...muted, fontSize: 12 }}>{t.offNote}</div> : null}
    </Card>
  );
}

/** The model workers of docs/design/remote-agents.md: one fresh ping per enabled host, read-only. */
export function WorkersSection({ state }: { state: HubDataState<WorkerReport> }) {
  const t = tr.debate.workers;
  return (
    <Panel
      title={t.title}
      actions={
        <Button size="sm" variant="outline" disabled={state.loading} onClick={state.reload}>
          {tr.common.refresh}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-6)">
        <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{t.intro}</div>
        <DataGate state={state} what={t.what}>
          {(data) => (
            <>
              {data.error ? <Notice tone="crit">{data.error}</Notice> : null}
              {data.workers.length ? (
                <Grid min={300} gap="var(--sg-s-6)">
                  {data.workers.map((w) => (
                    <WorkerCard key={w.id} w={w} codeVersion={data.code_version} />
                  ))}
                </Grid>
              ) : (
                <div style={muted}>{t.empty}</div>
              )}
            </>
          )}
        </DataGate>
      </Stack>
    </Panel>
  );
}

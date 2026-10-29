import { tr } from '../../i18n';
import { useEffect, useRef } from 'react';
import { Panel, StatusChip } from '@server-guardian/ui';
import { fetchAudit } from '../../api';
import { DataGate } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Disclosure } from '../../components/Disclosure';
import { mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { auditStatus, exitSummary, routeLabel } from '../../lib/audit';
import { fmtTime } from '../../lib/debate';
import type { AuditEntry } from '../../types';

export const HISTORY_LIMIT = 20;

function Targets({ targets }: { targets: string[] }) {
  if (targets.length <= 1) return <span style={mono}>{targets[0] ?? '—'}</span>;
  return (
    <span title={targets.join(', ')}>
      <Disclosure summary={tr.ops.run.history.targetsCount(targets.length)}>
        <span style={{ ...mono, fontSize: 12, overflowWrap: 'anywhere' }}>{targets.join(', ')}</span>
      </Disclosure>
    </span>
  );
}

const COLUMNS: Column<AuditEntry>[] = [
  { header: tr.ops.run.history.time, render: (e) => <span style={{ whiteSpace: 'nowrap' }}>{fmtTime(e.ts)}</span> },
  { header: tr.ops.run.history.route, render: (e) => routeLabel(e.route) },
  { header: tr.ops.run.history.targets, render: (e) => <Targets targets={e.targets ?? []} /> },
  {
    header: tr.ops.run.history.command,
    mono: true,
    render: (e) => (
      <span style={{ overflowWrap: 'anywhere' }} title={e.command}>
        {e.command || (e.detail ? '' : '—')}
        {e.detail ? <span style={{ ...muted, display: 'block', fontSize: 12 }}>{e.detail}</span> : null}
      </span>
    ),
  },
  {
    header: tr.ops.run.history.result,
    render: (e) => {
      const st = auditStatus(e.status);
      return (
        <span style={{ display: 'inline-flex', gap: 'var(--sg-s-3)', alignItems: 'center', flexWrap: 'wrap' }}>
          <StatusChip tone={st.tone}>{st.label}</StatusChip>
          {e.command ? <span style={{ ...muted, ...mono, fontSize: 12 }}>{exitSummary(e)}</span> : null}
        </span>
      );
    },
  },
];

/**
 * Recent commands the hub ran (its audit log), newest first. `refreshKey` changes when a run ends so the
 * entry of that run shows up without a manual refresh.
 */
export function CommandHistory({ refreshKey }: { refreshKey: string }) {
  const state = useHubData(() => fetchAudit(HISTORY_LIMIT));
  const reload = state.reload;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reload();
  }, [refreshKey, reload]);
  return (
    <Panel title={tr.ops.run.history.title}>
      <DataGate state={state} what={tr.ops.run.history.what}>
        {(entries) => <DataTable label={tr.ops.run.history.label} columns={COLUMNS} rows={entries} getKey={(e, i) => `${e.ts}:${i}`} empty={tr.ops.run.history.empty} />}
      </DataGate>
    </Panel>
  );
}

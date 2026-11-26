import { useMemo, useState } from 'react';
import { SelectField } from '../../components/SelectField';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Row, muted } from '../../components/layout';
import { Ago } from '../../components/Ago';
import { parseTime } from '../../lib/debate';
import type { ResidentEvent } from '../../types';
import { tr } from '../../i18n';
import { detailPairs } from './format';

/**
 * The recent store events, newest first, filtered by agent and by kind (both built from what the
 * snapshot holds). `detail` is writer-defined ids, counts and reasons, shown as compact pairs.
 */
export function EventsFeed({ events, agents }: { events: ResidentEvent[]; agents: string[] }) {
  const [agent, setAgent] = useState('');
  const [kind, setKind] = useState('');

  const kinds = useMemo(() => Array.from(new Set(events.map((e) => e.kind).filter((k): k is string => !!k))).sort(), [events]);
  const shown = useMemo(
    () =>
      // The snapshot already lists events newest first (ORDER BY id DESC).
      events.filter((e) => (!agent || e.agent === agent) && (!kind || e.kind === kind)),
    [events, agent, kind],
  );

  const columns: Column<ResidentEvent>[] = [
    {
      header: tr.residents.colAt,
      nowrap: true,
      render: (e) => {
        const t = e.at ? parseTime(e.at) : null;
        return t !== null ? <Ago at={t} /> : <span style={muted}>{e.at ?? tr.common.dash}</span>;
      },
    },
    { header: tr.residents.colKind, nowrap: true, mono: true, render: (e) => e.kind ?? tr.common.dash },
    { header: tr.residents.colAgent, nowrap: true, mono: true, render: (e) => e.agent ?? tr.common.dash },
    {
      header: tr.residents.colDetail,
      minWidth: 200,
      mono: true,
      render: (e) => <span style={{ ...muted, fontSize: 12 }}>{detailPairs(e.detail) || tr.common.dash}</span>,
    },
  ];

  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-5)', minWidth: 0 }}>
      <Row gap="var(--sg-s-6)" style={{ alignItems: 'flex-end' }}>
        <SelectField
          label={tr.residents.filterAgent}
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          options={[{ value: '', label: tr.residents.filterAll }, ...agents.map((a) => ({ value: a, label: a }))]}
        />
        <SelectField
          label={tr.residents.filterKind}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          options={[{ value: '', label: tr.residents.filterAll }, ...kinds.map((k) => ({ value: k, label: k }))]}
        />
      </Row>
      <DataTable label={tr.residents.eventsTitle} columns={columns} rows={shown} getKey={(e, i) => String(e.id ?? i)} empty={tr.residents.noEvents} />
    </div>
  );
}

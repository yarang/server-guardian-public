import { useMemo, useState } from 'react';
import { fetchFlows, fetchPersonas } from '../../api';
import { Disclosure } from '../../components/Disclosure';
import { Notice } from '../../components/Notice';
import { Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { BUILTIN_PROVIDER_LABEL } from '../../lib/flowRun';
import type { DebateJobDetail } from '../../types';
import { FlowCanvas } from './FlowCanvas';
import type { SeatState } from './FlowCanvas';
import { tr } from '../../i18n';

const t = () => tr.flows.view;

/** The flow a debate started from, drawn read-only with the seats that ran coloured (failed: answered by the fallback). */
export function FlowRunView({ job }: { job: DebateJobDetail }) {
  const team = job.result && typeof job.result.team === 'object' ? job.result.team : null;
  const record = team?.flow_graph;
  const id = record?.id ?? job.flow;
  // The diagram is drawn once opened: xyflow fits the view to its size, which is zero while collapsed.
  const [open, setOpen] = useState(false);
  const flows = useHubData(fetchFlows);
  const personas = useHubData(fetchPersonas);
  const entry = flows.data?.flows.find((f) => f.id === id);
  const names = useMemo(() => Object.fromEntries((personas.data?.personas ?? []).map((p) => [p.id, p.name || p.id])), [personas.data]);
  const states = useMemo(() => {
    const out: Record<string, SeatState> = {};
    for (const item of job.result?.transcript ?? []) {
      if (!item.seat) continue;
      out[item.seat] = item.provider === BUILTIN_PROVIDER_LABEL ? 'failed' : out[item.seat] ?? 'ran';
    }
    return out;
  }, [job.result]);
  // The characters that actually sat (after swaps at start) replace the flow's defaults in the drawing.
  const flow = useMemo(() => {
    if (!entry) return null;
    const seated = record?.seats ?? {};
    return {
      ...entry.data,
      seats: entry.data.seats.map((s) => (seated[s.id] ? { ...s, character: seated[s.id] as string } : s)),
      close: seated.close ? { ...entry.data.close, character: seated.close as string } : entry.data.close,
    };
  }, [entry, record]);
  if (!id) return null;
  return (
    <Disclosure summary={t().title} open={open} onToggle={setOpen}>
      <Stack gap="var(--sg-s-3)">
        <div style={{ ...muted, fontSize: 12 }}>{t().note(id)}</div>
        {flows.data && !entry ? <Notice tone="warn">{t().missing(id)}</Notice> : null}
        {entry && record && entry.hash !== record.hash ? <Notice tone="info">{t().changed}</Notice> : null}
        {flow && open ? <FlowCanvas flow={flow} names={names} states={states} height={360} /> : null}
      </Stack>
    </Disclosure>
  );
}

import { tr } from '../../i18n';
import { Meter, Panel, ProcessTable, StatCard } from '@server-guardian/ui';
import { Grid, Stack } from '../../components/layout';
import { duration, fixed, gb, pct } from '../../lib/format';
import { USAGE_THRESHOLDS, usageTone } from '../../lib/tones';
import type { SystemMetrics } from '../../types';

function UsageCard({ label, value, caption }: { label: string; value: number; caption?: string }) {
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
      <StatCard label={label} value={fixed(value, 1)} unit="%" caption={caption} tone={usageTone(value)} />
      <Meter value={pct(value)} aria-label={tr.fleet.hub.usageAria(label)} thresholds={USAGE_THRESHOLDS} showValue={false} />
    </div>
  );
}

/** Full hub-host metrics: usage meters, load, uptime and the top processes. */
export function HubHostSection({ m }: { m: SystemMetrics }) {
  const [l1, l5, l15] = m.load_avg ?? [];
  const processes = (m.top_processes ?? []).map((p) => ({ pid: p.pid, user: p.user, cpu: p.cpu, mem: p.mem, command: p.command }));
  return (
    <Stack>
      <Grid min={220}>
        <UsageCard label={tr.fleet.hub.cpu} value={m.cpu_usage_pct} caption={m.hostname} />
        <UsageCard label={tr.fleet.hub.memory} value={m.memory_pct} caption={`${gb(m.memory_used_bytes)} / ${gb(m.memory_total_bytes)} GB`} />
        <UsageCard label={tr.fleet.hub.disk} value={m.disk_pct} caption={`${gb(m.disk_used_bytes, 0)} / ${gb(m.disk_total_bytes, 0)} GB`} />
        <StatCard label={tr.fleet.hub.loadAverage} value={fixed(l1, 2)} caption={tr.fleet.hub.loadCaption(fixed(l5, 2), fixed(l15, 2))} />
        <StatCard label={tr.fleet.hub.hostUptime} value={duration(m.uptime_sec)} caption={tr.fleet.hub.sinceBoot} />
      </Grid>
      {m.issues?.length ? (
        <div role="status" style={{ color: 'var(--sg-warn)', fontSize: 13 }}>
          {tr.fleet.hub.unavailable(m.issues.join('; '))}
        </div>
      ) : null}
      <Panel title={tr.fleet.hub.topProcesses} flush>
        {processes.length ? <ProcessTable rows={processes} /> : <div style={{ padding: 'var(--sg-s-7)' }}>{tr.fleet.hub.noProcesses}</div>}
      </Panel>
    </Stack>
  );
}

import { tr } from '../i18n';
import { isOnline, reachabilityInfo } from '../lib/reachability';
import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { Button, Panel, StatCard, StatusChip, StatusDot } from '@server-guardian/ui';
import type { Tone } from '@server-guardian/ui';
import { fetchMetrics } from '../api';
import { Ago } from '../components/Ago';
import { CheckboxField } from '../components/CheckboxField';
import { DataGate, ErrorNotice } from '../components/DataGate';
import { DataTable } from '../components/DataTable';
import { MeshNodesPanel } from './nodes/MeshNodesPanel';
import type { Column } from '../components/DataTable';
import { Notice } from '../components/Notice';
import { SelectField } from '../components/SelectField';
import { Grid, Row, Stack, muted } from '../components/layout';
import { useHubData } from '../hooks/useHubData';
import { useNarrow } from '../hooks/useNarrow';
import type { HubDataState } from '../hooks/useHubData';
import { fixed, shortUptime } from '../lib/format';
import { GROUP_BY_OPTIONS, UNGROUPED, UNGROUPED_KEY, bucketBy, filterByGroup, groupNamesById, serverField } from '../lib/groups';
import type { GroupBy } from '../lib/groups';
import { fleetHref, navigateFleet } from '../router';
import { useHubStore } from '../state/hubStore';
import type { FleetNodeResult, GroupSaveResult, NodeGroup, NodeGroupList, FleetNode } from '../types';
import { AddToGroupDialog } from './fleet/AddToGroupDialog';
import { GroupDialog } from './fleet/GroupDialog';
import { GroupSummary } from './nodes/GroupSummary';
import { HubHostSection } from './nodes/HubHostSection';

const COLUMNS_KEY = 'guardian.nodes.detailColumns';

/** Whether the Arch, Mesh IP and Probe columns are shown; remembered per browser, off by default. */
function loadDetailColumns(): boolean {
  try {
    return window.localStorage.getItem(COLUMNS_KEY) === '1';
  } catch {
    return false;
  }
}

function storeDetailColumns(on: boolean): void {
  try {
    window.localStorage.setItem(COLUMNS_KEY, on ? '1' : '0');
  } catch {
    // Storage unavailable: the choice lasts until the page closes.
  }
}

/** Reachability filter of the inventory (`?status=`). */
type StatusFilter = 'all' | 'online' | 'offline';

const STATUS_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: tr.fleet.nodes.statusOptions.all },
  { value: 'online', label: tr.fleet.nodes.statusOptions.online },
  { value: 'offline', label: tr.fleet.nodes.statusOptions.offline },
];

/** Unknown `?status=` values are ignored rather than hiding every node. */
function parseStatus(value: string | undefined): StatusFilter {
  return value === 'online' || value === 'offline' ? value : 'all';
}

/** Reachability comes only from a fleet diagnosis probe; servers.json has no live status. */
type Reachability = { tone: Tone; label: string };

function reachability(id: string, probe: Map<string, FleetNodeResult> | null, running: boolean): Reachability {
  const r = probe?.get(id);
  if (r) return reachabilityInfo(r);
  if (running) return { tone: 'idle', label: tr.fleet.nodes.reach.checking };
  return { tone: 'idle', label: probe ? tr.fleet.nodes.reach.notProbed : tr.fleet.nodes.reach.notChecked };
}

const NOWRAP: CSSProperties = { whiteSpace: 'nowrap' };

/** Muted second line of a compact row: host · groups · 가동 8일 15시간 · 부하 0.01 · 메모리 34%. */
function summaryLine(s: FleetNode, probe: Map<string, FleetNodeResult> | null, groupsOf: Map<string, string[]>): string {
  const r = probe?.get(s.id);
  const parts = [s.hostname, (groupsOf.get(s.id) ?? []).join(', ') || tr.fleet.nodes.noGroups];
  if (r) {
    const up = shortUptime(r.uptime);
    if (up !== '—') parts.push(tr.fleet.nodes.uptimeShort(up));
    const load = r.load_avg?.trim().split(/[\s,]+/)[0];
    if (load) parts.push(tr.fleet.nodes.loadShort(load));
    if (r.memory) parts.push(tr.fleet.nodes.memShort(r.memory));
  }
  return parts.join(' · ');
}

/** Narrow-screen list: one line with select, node and status, one muted summary line; nothing wraps per character. */
function CompactNodes({
  label,
  rows,
  probe,
  running,
  groupsOf,
  selected,
  onSelect,
}: {
  label: string;
  rows: FleetNode[];
  probe: Map<string, FleetNodeResult> | null;
  running: boolean;
  groupsOf: Map<string, string[]>;
  selected: Set<string>;
  onSelect: (id: string, on: boolean) => void;
}) {
  return (
    <ul aria-label={label} style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', fontSize: 13 }}>
      {rows.map((s) => {
        const r = reachability(s.id, probe, running);
        const summary = summaryLine(s, probe, groupsOf);
        return (
          <li key={s.id} style={{ padding: 'var(--sg-s-3) 0', borderBottom: '1px solid var(--sg-border-subtle)', minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-3)', minWidth: 0 }}>
              <CheckboxField aria-label={tr.fleet.nodes.selectAria(s.name)} checked={selected.has(s.id)} onChange={(e) => onSelect(s.id, e.target.checked)}>
                {''}
              </CheckboxField>
              <a href={fleetHref('nodes', [s.id])} style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                <strong>{s.name}</strong>
              </a>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--sg-s-2)', whiteSpace: 'nowrap' }}>
                <StatusDot tone={r.tone} label={r.label} />
                <span aria-hidden="true">{r.label}</span>
              </span>
              {s.host_key_mode === 'accept-new' ? (
                <a href={fleetHref('nodes', [s.id])} title={tr.fleet.nodes.acceptNewTitle}>
                  <StatusChip tone="idle">accept-new</StatusChip>
                </a>
              ) : null}
            </div>
            <div
              data-testid="node-summary"
              title={summary}
              style={{ ...muted, fontSize: 12, fontFamily: 'var(--sg-font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            >
              {summary}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** Inventory columns with the live probe merged in; offline or unprobed nodes show a dash, never a guess. */
function serverColumns(
  probe: Map<string, FleetNodeResult> | null,
  running: boolean,
  groupsOf: Map<string, string[]>,
  selected: Set<string>,
  onSelect: (id: string, on: boolean) => void,
  detailColumns: boolean
): Column<FleetNode>[] {
  const probed = (s: FleetNode, pick: (r: FleetNodeResult) => string | undefined) => (probe?.get(s.id) ? pick(probe.get(s.id) as FleetNodeResult) || '—' : '—');
  return [
    {
      header: tr.fleet.nodes.columns.select,
      render: (s) => (
        <CheckboxField aria-label={tr.fleet.nodes.selectAria(s.name)} checked={selected.has(s.id)} onChange={(e) => onSelect(s.id, e.target.checked)}>
          {''}
        </CheckboxField>
      ),
    },
    {
      header: tr.fleet.nodes.columns.node,
      render: (s) => (
        <span style={{ display: 'inline-flex', alignItems: 'center', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
          <a href={fleetHref('nodes', [s.id])}>
            <strong>{s.name}</strong>
          </a>
          {s.connection === 'inventory' ? (
            <StatusChip tone="idle" title={tr.fleet.nodes.inventoryTitle}>
              inventory
            </StatusChip>
          ) : null}
          {s.host_key_mode === 'accept-new' ? (
            <a href={fleetHref('nodes', [s.id])} title={tr.fleet.nodes.acceptNewTitle}>
              <StatusChip tone="idle">accept-new</StatusChip>
            </a>
          ) : null}
        </span>
      ),
    },
    { header: tr.fleet.nodes.columns.host, mono: true, render: (s) => <span style={NOWRAP}>{s.hostname}</span> },
    ...(detailColumns
      ? [
          { header: tr.fleet.nodes.columns.arch, mono: true, render: (s: FleetNode) => s.arch },
          { header: tr.fleet.nodes.columns.meshIp, mono: true, render: (s: FleetNode) => <span style={NOWRAP}>{s.tailscale_ip ?? '—'}</span> },
        ]
      : []),
    { header: tr.fleet.nodes.columns.groups, render: (s) => (groupsOf.get(s.id) ?? []).join(', ') || '—' },
    {
      header: tr.fleet.nodes.columns.status,
      render: (s) => {
        const r = reachability(s.id, probe, running);
        return (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--sg-s-2)' }}>
            <StatusDot tone={r.tone} label={r.label} />
            <span aria-hidden="true">{r.label}</span>
          </span>
        );
      },
    },
    {
      header: tr.fleet.nodes.columns.uptime,
      mono: true,
      render: (s) => {
        const raw = probe?.get(s.id)?.uptime;
        return (
          <span style={NOWRAP} title={raw || undefined}>
            {raw ? shortUptime(raw) : '—'}
          </span>
        );
      },
    },
    { header: tr.fleet.nodes.columns.load, mono: true, render: (s) => probed(s, (r) => r.load_avg) },
    { header: tr.fleet.nodes.columns.memory, mono: true, render: (s) => probed(s, (r) => r.memory) },
    { header: tr.fleet.nodes.columns.disk, mono: true, render: (s) => probed(s, (r) => r.disk) },
    ...(detailColumns
      ? [{ header: tr.fleet.nodes.columns.probe, mono: true, align: 'right' as const, render: (s: FleetNode) => (probe?.get(s.id) ? `${probe.get(s.id)?.duration_ms} ms` : '—') }]
      : []),
  ];
}

/** Writes a filter into the URL; the route's query is the single source of truth for the filters. */
function setFilters(next: { status: StatusFilter; group: string }): void {
  const query: Record<string, string> = {};
  if (next.status !== 'all') query.status = next.status;
  if (next.group !== 'all') query.group = next.group;
  navigateFleet('nodes', [], query);
}

/**
 * Node inventory bucketed by user groups (a node shows under every group it is in), tier,
 * arch or not at all, narrowed by the `?group=` and `?status=` filters.
 */
function Inventory({
  servers,
  groups,
  probe,
  running,
  status,
  group,
}: {
  servers: FleetNode[];
  groups: HubDataState<NodeGroupList>;
  probe: Map<string, FleetNodeResult> | null;
  running: boolean;
  status: StatusFilter;
  group: string;
}) {
  const [by, setBy] = useState<GroupBy>('groups');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<'create' | 'add' | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [detailColumns, setDetailColumns] = useState(loadDetailColumns);
  const narrow = useNarrow();
  const list: NodeGroup[] = groups.data?.groups ?? [];
  const groupsMissing = groups.data === null;
  const byGroup = filterByGroup(servers, groupsMissing ? 'all' : group, list);
  // A status filter needs a probe; nodes without a result match neither online nor offline.
  const shown = status === 'all' || probe === null ? byGroup : byGroup.filter((s) => (probe.has(s.id) && isOnline(probe.get(s.id) as FleetNodeResult)) === (status === 'online') && probe.has(s.id));
  const shownKey = shown.map((s) => s.id).join(',');
  // The selection only ever holds visible rows: ids a filter hides are dropped.
  useEffect(() => {
    const visible = new Set(shownKey ? shownKey.split(',') : []);
    setSelected((prev) => (Array.from(prev).every((id) => visible.has(id)) ? prev : new Set(Array.from(prev).filter((id) => visible.has(id)))));
  }, [shownKey]);
  if (servers.length === 0) return <div style={{ color: 'var(--sg-text-muted)' }}>{tr.fleet.nodes.hubReportsNone}</div>;

  // Without group data, "User groups" would put every node under Ungrouped; show them ungrouped instead.
  const effectiveBy: GroupBy = by === 'groups' && groups.error !== null ? 'none' : by;
  const selectedIds = shown.filter((s) => selected.has(s.id)).map((s) => s.id);
  const nameOf = (id: string) => servers.find((s) => s.id === id)?.name ?? id;
  const select = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const allShown = shown.length > 0 && selectedIds.length === shown.length;
  const columns = serverColumns(probe, running, groupNamesById(list), selected, select, detailColumns);
  const saved = (message: string) => {
    setBanner(message);
    setDialog(null);
    setSelected(new Set());
    groups.reload();
  };
  const filterOptions = [
    { value: 'all', label: tr.fleet.nodes.allNodes },
    ...list.map((g) => ({ value: g.id, label: `${g.name} (${g.members.length})`, disabled: g.members.length === 0 })),
    { value: UNGROUPED_KEY, label: `${UNGROUPED} (${groups.data?.ungrouped.length ?? 0})` },
  ];
  // A ?group= id the hub does not know would leave the select blank; keep it selectable so the URL stays truthful.
  if (group !== 'all' && !filterOptions.some((o) => o.value === group)) filterOptions.push({ value: group, label: tr.fleet.nodes.unknownGroup(group), disabled: false });

  return (
    <Stack gap="var(--sg-s-8)">
      <Row gap="var(--sg-s-6)">
        <SelectField label={tr.fleet.nodes.groupBy} value={by} options={GROUP_BY_OPTIONS} onChange={(e) => setBy(e.target.value as GroupBy)} />
        <SelectField
          label={tr.fleet.nodes.groupFilter}
          value={groupsMissing ? 'all' : group}
          options={filterOptions}
          disabled={groupsMissing}
          onChange={(e) => setFilters({ status, group: e.target.value })}
        />
        <SelectField
          label={tr.fleet.nodes.statusFilter}
          value={status}
          options={STATUS_OPTIONS}
          onChange={(e) => setFilters({ status: e.target.value as StatusFilter, group })}
        />
        {narrow ? null : (
          <CheckboxField
            checked={detailColumns}
            onChange={(e) => {
              setDetailColumns(e.target.checked);
              storeDetailColumns(e.target.checked);
            }}
          >
            {tr.fleet.nodes.detailColumns}
          </CheckboxField>
        )}
      </Row>
      {banner ? <Notice tone="ok">{banner}</Notice> : null}
      {groups.error !== null ? (
        <ErrorNotice
          title={tr.fleet.nodes.groupsLoadFailed}
          message={groups.error}
          onRetry={groups.reload}
          retrying={groups.loading}
        />
      ) : null}
      {status !== 'all' && probe === null ? (
        <div role="status" style={{ color: 'var(--sg-text-muted)' }}>
          {running ? tr.fleet.nodes.waitingDiagnosis : tr.fleet.nodes.runDiagnosisToFilter}
        </div>
      ) : effectiveBy === 'groups' && groupsMissing ? (
        <div role="status" style={{ color: 'var(--sg-text-muted)' }}>
          {tr.fleet.nodes.loadingGroups}
        </div>
      ) : shown.length === 0 ? (
        <div style={{ color: 'var(--sg-text-muted)' }}>{status === 'all' ? tr.fleet.nodes.emptyGroup : tr.fleet.nodes.emptyStatus(status, group !== 'all')}</div>
      ) : (
        <>
          <Row gap="var(--sg-s-6)">
            <CheckboxField
              aria-label={tr.fleet.nodes.selectAllAria}
              checked={allShown}
              onChange={(e) => setSelected(e.target.checked ? new Set(shown.map((s) => s.id)) : new Set())}
            >
              {tr.fleet.nodes.selectAll(shown.length)}
            </CheckboxField>
          </Row>
          {selectedIds.length > 0 ? (
            <section
              aria-label={tr.fleet.nodes.selectionAria}
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                gap: 'var(--sg-s-4)',
                padding: 'var(--sg-s-4) var(--sg-s-5)',
                border: '1px solid var(--sg-border)',
                background: 'var(--sg-surface-sunken)',
                borderRadius: 'var(--sg-r-md)',
              }}
            >
              <strong>{tr.fleet.nodes.selected(selectedIds.length)}</strong>
              <span style={{ ...muted, fontSize: 12 }}>{tr.fleet.nodes.selectionHint}</span>
              <Button size="sm" variant="primary" disabled={groupsMissing} onClick={() => setDialog('create')}>
                {tr.fleet.nodes.createFromSelection}
              </Button>
              <Button size="sm" variant="outline" disabled={groupsMissing || list.length === 0} onClick={() => setDialog('add')}>
                {tr.fleet.nodes.addToGroup}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
                {tr.fleet.nodes.clearSelection}
              </Button>
            </section>
          ) : null}
          {bucketBy(shown, (s) => s.id, effectiveBy, list, serverField).map((b) => (
          <div key={b.key} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)', alignItems: 'baseline' }}>
              <strong>{b.label}</strong>
              <span style={{ color: 'var(--sg-text-muted)', fontSize: 12 }}>{tr.fleet.nodes.nodeCount(b.items.length)}</span>
            </div>
            {narrow ? (
              <CompactNodes label={tr.fleet.nodes.nodesAria(b.label)} rows={b.items} probe={probe} running={running} groupsOf={groupNamesById(list)} selected={selected} onSelect={select} />
            ) : (
              <DataTable label={tr.fleet.nodes.nodesAria(b.label)} columns={columns} rows={b.items} getKey={(s) => s.id} />
            )}
          </div>
          ))}
        </>
      )}
      {dialog === 'create' && groups.data ? (
        <GroupDialog
          group={null}
          initialInclude={selectedIds}
          servers={servers}
          fields={groups.data.catalog?.fields ?? []}
          onClose={() => setDialog(null)}
          onSaved={(res: GroupSaveResult) => saved(tr.fleet.nodes.savedGroup(res.id, res.members.length))}
          onDeleted={() => undefined}
        />
      ) : null}
      {dialog === 'add' ? (
        <AddToGroupDialog
          groups={list}
          ids={selectedIds}
          nameOf={nameOf}
          onClose={() => setDialog(null)}
          onSaved={(g, res) => saved(tr.fleet.nodes.updatedGroup(g.name, res.members.length))}
        />
      ) : null}
    </Stack>
  );
}

/**
 * Nodes: the node inventory with the shared fleet probe merged in, the hub's own metrics and
 * node group management. `query` carries the `status` and `group` filters from the URL.
 */
export function NodesPage({ query }: { query: Record<string, string> }) {
  const metrics = useHubData(fetchMetrics, { pollMs: 5000 });
  const { servers, groups, diagnosis } = useHubStore();

  const report = diagnosis.data;
  const results = report?.results ?? [];
  const probe = report ? new Map(results.map((r) => [r.id, r])) : null;
  const online = results.length ? results.filter(isOnline).length : report?.online_count ?? 0;

  const groupId = query.group || 'all';
  const summaryGroup = groups.data?.groups.find((g) => g.id === groupId) ?? null;

  return (
    <Stack>
      {summaryGroup ? <GroupSummary group={summaryGroup} servers={servers.data ?? []} diagnosis={diagnosis} /> : null}

      <Panel
        title={tr.fleet.nodes.inventoryPanel(servers.data?.length)}
        actions={
          <Row>
            <Button size="sm" variant="outline" onClick={() => navigateFleet('nodes', ['new'])}>
              {tr.fleet.nodes.addNode}
            </Button>
            {diagnosis.measuredAt !== null ? (
              <span style={{ color: 'var(--sg-text-muted)', fontSize: 12 }}>
                <Ago at={diagnosis.measuredAt} prefix={tr.fleet.nodes.probedPrefix} />
              </span>
            ) : null}
            <Button size="sm" variant="primary" loading={diagnosis.loading} onClick={diagnosis.rerun}>
              {report ? tr.fleet.nodes.rerunDiagnosis : tr.fleet.nodes.runDiagnosis}
            </Button>
          </Row>
        }
      >
        <Stack>
          {diagnosis.error !== null ? (
            <ErrorNotice title={tr.fleet.nodes.diagnosisFailed} message={diagnosis.error} onRetry={diagnosis.rerun} retrying={diagnosis.loading} />
          ) : null}
          {report ? (
            <Grid min={180}>
              <StatCard
                label={tr.fleet.nodes.nodesOnline}
                value={online}
                unit={`/ ${report.total_nodes}`}
                caption={online === report.total_nodes ? tr.fleet.nodes.allReachable : tr.fleet.nodes.notOnline(report.total_nodes - online)}
                tone={online === report.total_nodes ? 'ok' : 'crit'}
              />
              <StatCard label={tr.fleet.nodes.probeDuration} value={fixed(report.duration_sec, 2)} unit="s" />
            </Grid>
          ) : null}
          <DataGate state={servers} what={tr.fleet.nodes.inventoryWhat}>
            {(list) => (
              <Inventory
                servers={list}
                groups={groups}
                probe={probe}
                running={diagnosis.loading}
                status={parseStatus(query.status)}
                group={groupId}
              />
            )}
          </DataGate>
        </Stack>
      </Panel>

      <MeshNodesPanel />

      <Panel title={tr.fleet.nodes.hub}>
        <DataGate state={metrics} what={tr.fleet.nodes.hubMetricsWhat}>
          {(m) => <HubHostSection m={m} />}
        </DataGate>
      </Panel>

      <div style={{ fontSize: 13 }}>
        <a href={fleetHref('groups')}>{tr.fleet.nodes.manageGroups}</a>
        {tr.fleet.nodes.manageGroupsHint}
      </div>
    </Stack>
  );
}

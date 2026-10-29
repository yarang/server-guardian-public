import { tr } from '../../i18n';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Heatmap, Panel, StatCard, StatusChip } from '@server-guardian/ui';
import { fetchFleetSSHAudit } from '../../api';
import { ErrorNotice, LoadingNote } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Notice, NoticeList } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { Grid, Row, Stack } from '../../components/layout';
import { useHubAction } from '../../hooks/useHubAction';
import { fmtDateTime } from '../../lib/format';
import type { HubDataState } from '../../hooks/useHubData';
import { useRefreshToken } from '../../hooks/refresh';
import { UNGROUPED, UNGROUPED_KEY, bucketBy, filterByGroup } from '../../lib/groups';
import { findings, groupAuditRows, heatmapColumns, heatmapRows, isFailing, nodeStatusTone, ruleColumns } from '../../lib/sshAudit';
import type { GroupAuditRow } from '../../lib/sshAudit';
import { rememberAudit } from '../../lib/auditCache';
import { reachabilityInfo } from '../../lib/reachability';
import { buildNodeDebateTask } from '../../lib/debateTask';
import { fleetHref, navigate } from '../../router';
import { setDebatePrefill } from '../../state/debatePrefill';
import { useHubStore } from '../../state/hubStore';
import { scoreTone } from '../../lib/tones';
import type { FleetSSHNode, FleetSSHReport, NodeGroup, NodeGroupList } from '../../types';
import { KEY_COLUMNS, RuleList } from './shared';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} style={{ display: 'grid', gap: 'var(--sg-s-4)', minWidth: 0 }}>
      <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{title}</h3>
      {children}
    </section>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return <div style={{ color: 'var(--sg-text-muted)', fontSize: 13 }}>{children}</div>;
}

/** Node name linking to its detail page (`#/fleet/nodes/:id`). */
function NodeLink({ id, name }: { id: string; name: string }) {
  return <a href={fleetHref('nodes', [id])}>{name}</a>;
}

function ScoreChip({ score }: { score: number | null | undefined }) {
  if (score === null || score === undefined) return <span style={{ color: 'var(--sg-text-muted)' }}>—</span>;
  return <StatusChip tone={scoreTone(score)}>{String(score)}</StatusChip>;
}

function groupColumns(): Column<GroupAuditRow>[] {
  return [
    { header: tr.fleet.ssh.groupColumns.group, render: (r) => <strong>{r.group}</strong> },
    { header: tr.fleet.ssh.groupColumns.nodes, align: 'right', render: (r) => r.nodes },
    {
      header: tr.fleet.ssh.groupColumns.audited,
      align: 'right',
      render: (r) => (r.unreachable ? tr.fleet.ssh.auditedWithUnreachable(r.audited, r.unreachable) : r.audited),
    },
    { header: tr.fleet.ssh.groupColumns.avgScore, render: (r) => <ScoreChip score={r.avgScore} /> },
    {
      header: tr.fleet.ssh.groupColumns.failingNodes,
      render: (r) =>
        r.failing.length ? (
          <>
            {`${r.failing.length}: `}
            {r.failing.map((n, i) => (
              <span key={n.id}>
                {i > 0 ? ', ' : ''}
                <NodeLink id={n.id} name={n.name} />
              </span>
            ))}
          </>
        ) : (
          '0'
        ),
    },
    { header: tr.fleet.ssh.groupColumns.worstRule, render: (r) => (r.worst ? tr.fleet.ssh.worst(r.worst.column.label, r.worst.fail) : '—') },
  ];
}

/** One node's audit: status, score, notices, findings and authorized keys. Shared with the node detail page. */
export function NodeDetails({ node, link = true, onDebate }: { node: FleetSSHNode; link?: boolean; onDebate?: () => void }) {
  const rules = findings(node);
  return (
    <Stack gap="var(--sg-s-5)">
      <Row>
        <StatusChip tone={nodeStatusTone(node.status)} title={node.status}>
          {tr.fleet.ssh.nodeStatus[node.status] ?? node.status}
        </StatusChip>
        {node.reachability && node.reachability !== 'ok' ? (
          <StatusChip tone={reachabilityInfo(node).tone}>{reachabilityInfo(node).label}</StatusChip>
        ) : null}
        {node.report ? <ScoreChip score={node.report.score} /> : null}
        {link ? <a href={fleetHref('nodes', [node.id])}>{tr.fleet.ssh.openNode(node.name)}</a> : null}
        {onDebate ? (
          <Button size="sm" variant="outline" onClick={onDebate}>
            {tr.fleet.ssh.debateNode}
          </Button>
        ) : null}
        <span style={{ fontFamily: 'var(--sg-font-mono)', fontSize: 12, color: 'var(--sg-text-secondary)' }}>
          {[node.source, node.login_user ? tr.fleet.ssh.login(node.login_user) : '', `${node.duration_ms} ms`].filter(Boolean).join(' · ')}
        </span>
      </Row>
      {node.error ? <Notice tone="crit">{node.error}</Notice> : null}
      {node.config?.syntax_checked && !node.config.is_valid ? (
        <Notice tone="crit">{tr.fleet.ssh.sshdRejected(node.config.syntax_message)}</Notice>
      ) : null}
      <NoticeList tone="warn" items={node.notes} />
      {node.report ? (
        <RuleList rules={rules} empty={tr.fleet.ssh.allPass} />
      ) : (
        <Muted>{tr.fleet.ssh.noFindings}</Muted>
      )}
      <div style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
        <strong style={{ fontSize: 13 }}>{tr.fleet.ssh.nodeKeys(node.keys.length)}</strong>
        <DataTable
          label={tr.fleet.ssh.nodeKeysLabel(node.name)}
          columns={KEY_COLUMNS}
          rows={node.keys}
          getKey={(k, i) => `${k.fingerprint}-${i}`}
          empty={node.report ? tr.fleet.ssh.nodeKeysEmpty : tr.fleet.ssh.notRead}
        />
      </div>
    </Stack>
  );
}

/** "호스트 키 변경 1개 · 접속 불가 1개 · 오류 2개": the failing nodes split by the hub's reachability class. */
function reachabilityCaption(report: FleetSSHReport, errors: number): string {
  const counts = new Map<string, number>();
  for (const n of report.nodes) {
    if (!n.reachability || n.reachability === 'ok' || n.reachability === 'command_failed') continue;
    const label = reachabilityInfo(n).label;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  if (counts.size === 0) return tr.fleet.ssh.reachabilityFallback(report.summary.unreachable, errors);
  const parts = [...counts].map(([label, n]) => tr.fleet.ssh.reachabilityCount(label, n));
  if (errors) parts.push(tr.fleet.ssh.errorsCount(errors));
  return parts.join(' · ');
}

function FleetAuditBody({ report, groups, refreshing }: { report: FleetSSHReport; groups: HubDataState<NodeGroupList>; refreshing: boolean }) {
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState<string | null>(null);
  const { servers, diagnosis } = useHubStore();
  const s = report.summary;
  const columns = ruleColumns(report);
  const list: NodeGroup[] = groups.data?.groups ?? [];
  const groupsFailed = groups.error !== null;
  const groupsReady = groups.data !== null;

  const shown = filterByGroup(report.nodes, groupsReady ? filter : 'all', list);
  const selectedNode = shown.find((n) => n.id === selected) ?? shown.find(isFailing) ?? shown[0];

  const filterOptions = [
    { value: 'all', label: tr.fleet.ssh.allNodes },
    ...list.map((g) => ({ value: g.id, label: `${g.name} (${g.members.length})`, disabled: g.members.length === 0 })),
    { value: UNGROUPED_KEY, label: `${UNGROUPED} (${groups.data?.ungrouped.length ?? 0})` },
  ];
  const heatmapBuckets =
    filter === 'all' && groupsReady
      ? bucketBy(shown, (n) => n.id, 'groups', list)
      : [{ key: 'shown', label: filter === 'all' ? tr.fleet.ssh.allNodes : filterOptions.find((o) => o.value === filter)?.label ?? filter, items: shown }];
  const hmColumns = heatmapColumns(columns);

  // Hand the selected node's findings to the Debates page; needs the node's inventory entry for the task text.
  const debateNode = (node: FleetSSHNode) => {
    const inventory = servers.data?.find((n) => n.id === node.id);
    if (!inventory) return undefined;
    return () => {
      setDebatePrefill(
        buildNodeDebateTask(inventory, diagnosis.data?.results?.find((r) => r.id === node.id) ?? null, node),
        'ssh-audit',
        { node: node.id },
      );
      navigate('debates');
    };
  };

  return (
    <Stack gap="var(--sg-s-8)">
      <div role="status" style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>
        {refreshing
          ? tr.fleet.ssh.reauditing
          : tr.fleet.ssh.auditedLine(fmtDateTime(report.generated_at), report.cached, (report.duration_ms / 1000).toFixed(1))}
      </div>

      {s.nodes === 0 ? (
        <Muted>{tr.fleet.ssh.noNodesToAudit}</Muted>
      ) : (
        <>
          <Grid min={180}>
            <StatCard
              label={tr.fleet.ssh.stats.audited}
              value={s.audited}
              unit={`/ ${s.nodes}`}
              caption={s.unreachable || s.errors ? reachabilityCaption(report, s.errors) : tr.fleet.ssh.stats.allAnswered}
              tone={s.unreachable || s.errors ? 'crit' : 'ok'}
            />
            <StatCard
              label={tr.fleet.ssh.stats.avgScore}
              value={s.avg_score ?? '—'}
              unit={s.avg_score === null ? undefined : '/ 100'}
              caption={s.avg_score === null ? tr.fleet.ssh.stats.noNodeAudited : tr.fleet.ssh.stats.overAudited}
              tone={s.avg_score === null ? 'idle' : scoreTone(s.avg_score)}
            />
            <StatCard
              label={tr.fleet.ssh.stats.failingNodes}
              value={s.failing_nodes.length}
              caption={tr.fleet.ssh.stats.atLeastOneFail}
              tone={s.failing_nodes.length ? 'crit' : 'ok'}
            />
          </Grid>

          {groupsFailed ? (
            <ErrorNotice
              title={tr.fleet.ssh.groupsFailed}
              message={groups.error ?? ''}
              onRetry={groups.reload}
              retrying={groups.loading}
            />
          ) : null}

          <Section title={tr.fleet.ssh.byGroup}>
            {!groupsReady && !groupsFailed ? (
              <LoadingNote what={tr.fleet.ssh.groupsWhat} />
            ) : (
              <DataTable
                label={tr.fleet.ssh.byGroupLabel}
                columns={groupColumns()}
                rows={groupAuditRows(report, groupsReady ? list : null, columns)}
                getKey={(r) => r.key}
              />
            )}
          </Section>

          <Row gap="var(--sg-s-6)">
            <SelectField
              label={tr.fleet.ssh.groupFilter}
              value={groupsReady ? filter : 'all'}
              options={filterOptions}
              disabled={!groupsReady}
              onChange={(e) => setFilter(e.target.value)}
            />
          </Row>

          {shown.length === 0 ? (
            <Muted>{tr.fleet.ssh.noNodesInGroup}</Muted>
          ) : (
            <>
              <Section title={tr.fleet.ssh.rulesByNode}>
                <Muted>{tr.fleet.ssh.rulesHint}</Muted>
                {heatmapBuckets.map((b) => (
                  <div key={b.key} style={{ display: 'grid', gap: 'var(--sg-s-3)', minWidth: 0 }}>
                    {heatmapBuckets.length > 1 ? (
                      <div style={{ display: 'flex', gap: 'var(--sg-s-4)', alignItems: 'baseline' }}>
                        <strong>{b.label}</strong>
                        <span style={{ color: 'var(--sg-text-muted)', fontSize: 12 }}>
                          {tr.fleet.ssh.auditedOf(b.items.filter((n) => n.report).length, b.items.length)}
                        </span>
                      </div>
                    ) : null}
                    <Heatmap
                      columns={hmColumns}
                      rows={heatmapRows(b.items, columns)}
                      rowLabel={tr.fleet.ssh.rowLabel}
                      legend
                      selectedId={selectedNode?.id}
                      onSelectRow={setSelected}
                    />
                  </div>
                ))}
                {shown.some((n) => n.status === 'partial') ? (
                  <Muted>{tr.fleet.ssh.partialNote}</Muted>
                ) : null}
              </Section>

              {selectedNode ? (
                <Section title={tr.fleet.ssh.nodeSection(selectedNode.name)}>
                  <NodeDetails node={selectedNode} onDebate={debateNode(selectedNode)} />
                </Section>
              ) : null}
            </>
          )}
        </>
      )}
    </Stack>
  );
}

/**
 * Fleet-wide sshd audit (GET /api/fleet/ssh): per-group summary, rules-by-node heatmap and node
 * details. Loads once on open and on the global Refresh (the hub may answer from its 60 s cache);
 * "Refresh audit" asks the hub to probe every node again.
 */
export function FleetSshAudit() {
  const audit = useHubAction(fetchFleetSSHAudit);
  const { groups } = useHubStore();
  const refreshToken = useRefreshToken();
  const run = audit.run;
  useEffect(() => {
    void run([], false);
  }, [run, refreshToken]);

  const report = audit.data;
  useEffect(() => {
    if (report) rememberAudit(report);
  }, [report]);
  return (
    <Panel
      title={tr.fleet.ssh.fleetPanel}
      actions={
        <Button size="sm" variant="primary" loading={audit.running} onClick={() => void run([], true)}>
          {tr.fleet.ssh.refresh}
        </Button>
      }
    >
      {audit.error !== null ? (
        <ErrorNotice title={tr.fleet.ssh.fleetFailed} message={audit.error} onRetry={() => void run([], true)} retrying={audit.running} />
      ) : report === null ? (
        <LoadingNote what={tr.fleet.ssh.fleetLoadingWhat} />
      ) : (
        <FleetAuditBody report={report} groups={groups} refreshing={audit.running} />
      )}
    </Panel>
  );
}

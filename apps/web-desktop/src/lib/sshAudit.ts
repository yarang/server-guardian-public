import { tr } from '../i18n';
import type { HeatmapCell, HeatmapColumn, HeatmapRow, Tone } from '@server-guardian/ui';
import type { FleetSSHNode, FleetSSHReport, FleetSSHStatus, NodeGroup, SecurityRuleCheck } from '../types';
import { bucketBy } from './groups';

/**
 * Display helpers for the fleet SSH audit (GET /api/fleet/ssh). The hub scores every node; these
 * only group, count and map statuses. Nodes without a report are never given cells or scores.
 */

/** Short column labels for the heatmap; unknown rules fall back to their title. */
const SHORT_LABELS: Record<string, string> = tr.fleet.ssh.shortLabels;

export interface RuleColumn {
  id: string;
  label: string;
  title: string;
}

export function ruleColumns(report: FleetSSHReport): RuleColumn[] {
  return report.summary.rules.map((r) => ({ id: r.rule_id, label: SHORT_LABELS[r.rule_id] ?? r.title, title: r.title }));
}

/** Heatmap columns: short label, "rule id: title" as the header tooltip. */
export function heatmapColumns(columns: RuleColumn[]): HeatmapColumn[] {
  return columns.map((c) => ({ key: c.id, label: c.label, title: `${c.id}: ${c.title}` }));
}

/**
 * INFO (e.g. the default port 22) gets its own info cell. A rule missing from a node's report was
 * not evaluated and shows as "not checked" rather than an invented pass.
 */
export function heatmapCell(status: SecurityRuleCheck['status'] | undefined): HeatmapCell {
  if (status === 'PASS') return 'P';
  if (status === 'FAIL') return 'F';
  if (status === 'WARN') return 'W';
  if (status === 'INFO') return 'I';
  return 'N';
}

const NODE_STATUS_TONE: Record<FleetSSHStatus, Tone> = { ok: 'ok', partial: 'warn', unreachable: 'crit', error: 'crit' };

/**
 * One heatmap row per node (ids are node ids, unique within one heatmap). Nodes without a report
 * become unavailable rows carrying their status and error instead of cells or a score; partial
 * nodes (read without root) get a "partial" chip.
 */
export function heatmapRows(nodes: FleetSSHNode[], columns: RuleColumn[]): HeatmapRow[] {
  return nodes.map((n) => {
    if (!n.report) {
      return { id: n.id, name: n.name, cells: [], unavailable: `${tr.fleet.ssh.nodeStatus[n.status] ?? n.status}: ${n.error || tr.fleet.ssh.notAudited}` };
    }
    const byId = new Map(n.report.rules.map((r) => [r.rule_id, r.status]));
    const row: HeatmapRow = { id: n.id, name: n.name, cells: columns.map((c) => heatmapCell(byId.get(c.id))), score: n.report.score };
    if (n.status === 'partial') row.status = { tone: NODE_STATUS_TONE.partial, label: tr.fleet.ssh.nodeStatus.partial };
    return row;
  });
}

export function nodeStatusTone(status: FleetSSHStatus): Tone {
  return NODE_STATUS_TONE[status];
}

export function isFailing(n: FleetSSHNode): boolean {
  return (n.report?.failed_count ?? 0) > 0;
}

/** Average score of audited nodes, one decimal; null when none was audited. */
export function averageScore(nodes: FleetSSHNode[]): number | null {
  const scores = nodes.flatMap((n) => (n.report ? [n.report.score] : []));
  if (!scores.length) return null;
  return Math.round((scores.reduce((a, b) => a + b, 0) / scores.length) * 10) / 10;
}

/** The rule failing on the most nodes (ties keep evaluation order), or null when none fails. */
export function worstRule(nodes: FleetSSHNode[], columns: RuleColumn[]): { column: RuleColumn; fail: number } | null {
  let best: { column: RuleColumn; fail: number } | null = null;
  for (const column of columns) {
    const fail = nodes.filter((n) => n.report?.rules.some((r) => r.rule_id === column.id && r.status === 'FAIL')).length;
    if (fail > 0 && (best === null || fail > best.fail)) best = { column, fail };
  }
  return best;
}

export interface GroupAuditRow {
  key: string;
  group: string;
  nodes: number;
  audited: number;
  unreachable: number;
  avgScore: number | null;
  failing: FleetSSHNode[];
  worst: { column: RuleColumn; fail: number } | null;
}

/**
 * Per user group summary; a node in several groups counts in each, nodes in none under Ungrouped.
 * Without group data (null) all nodes form one "All nodes" row instead of a misleading Ungrouped.
 */
export function groupAuditRows(report: FleetSSHReport, groups: NodeGroup[] | null, columns: RuleColumn[]): GroupAuditRow[] {
  return bucketBy(report.nodes, (n) => n.id, groups ? 'groups' : 'none', groups ?? []).map((b) => ({
    key: b.key,
    group: b.label,
    nodes: b.items.length,
    audited: b.items.filter((n) => n.report).length,
    unreachable: b.items.filter((n) => n.status === 'unreachable').length,
    avgScore: averageScore(b.items),
    failing: b.items.filter(isFailing),
    worst: worstRule(b.items, columns),
  }));
}

/** Rules worth showing for a node: FAIL first, then WARN, then INFO; passes left out. */
export function findings(n: FleetSSHNode): SecurityRuleCheck[] {
  const order: Record<string, number> = { FAIL: 0, WARN: 1, INFO: 2 };
  return (n.report?.rules ?? []).filter((r) => r.status !== 'PASS').sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
}

import { tr } from '../i18n';
import { isOnline } from './reachability';
import type { FleetDiagnosisReport, FleetNodeResult, GroupCondition, GroupOp, NodeGroup, FleetNode } from '../types';

/**
 * Display helpers for user node groups. Membership itself is resolved by the hub (Python,
 * `members` in /api/groups); nothing here evaluates rules.
 */

export const UNGROUPED = tr.fleet.groupsLib.ungrouped;
export const UNGROUPED_KEY = '__ungrouped';

export type GroupBy = 'groups' | 'none' | 'tier' | 'arch';

export const GROUP_BY_OPTIONS: Array<{ value: GroupBy; label: string }> = [
  { value: 'groups', label: tr.fleet.groupsLib.groupBy.groups },
  { value: 'none', label: tr.fleet.groupsLib.groupBy.none },
  { value: 'tier', label: tr.fleet.groupsLib.groupBy.tier },
  { value: 'arch', label: tr.fleet.groupsLib.groupBy.arch },
];

export interface Bucket<T> {
  key: string;
  label: string;
  items: T[];
}

/** Node id -> names of the groups it belongs to (a node can be in several). */
export function groupNamesById(groups: NodeGroup[]): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const g of groups) {
    for (const id of g.members) out.set(id, [...(out.get(id) ?? []), g.name]);
  }
  return out;
}

/**
 * Buckets items for display. With user groups an item appears under every group it belongs to,
 * and items in no group go to "Ungrouped"; empty groups are left out.
 */
export function bucketBy<T>(items: T[], idOf: (item: T) => string, by: GroupBy, groups: NodeGroup[], fieldOf?: (item: T, field: 'tier' | 'arch') => string | undefined): Bucket<T>[] {
  if (by === 'none') return items.length ? [{ key: 'all', label: tr.fleet.groupsLib.allNodes, items }] : [];
  if (by === 'tier' || by === 'arch') {
    const map = new Map<string, T[]>();
    for (const item of items) {
      const key = fieldOf?.(item, by) || tr.fleet.groupsLib.noField(by);
      map.set(key, [...(map.get(key) ?? []), item]);
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, list]) => ({ key, label: key, items: list }));
  }
  const byId = new Map(items.map((i) => [idOf(i), i]));
  const grouped = new Set<string>();
  const buckets: Bucket<T>[] = [];
  for (const g of groups) {
    const list = g.members.map((id) => byId.get(id)).filter((i): i is T => i !== undefined);
    list.forEach((i) => grouped.add(idOf(i)));
    if (list.length) buckets.push({ key: g.id, label: g.name, items: list });
  }
  const rest = items.filter((i) => !grouped.has(idOf(i)));
  if (rest.length) buckets.push({ key: UNGROUPED_KEY, label: UNGROUPED, items: rest });
  return buckets;
}

export function serverField(s: FleetNode, field: 'tier' | 'arch'): string | undefined {
  return field === 'tier' ? s.tier : s.arch;
}

/** Keeps the nodes (or any items with a node id) of one group ('all' keeps everything, UNGROUPED_KEY those in no group). */
export function filterByGroup<T extends { id: string }>(servers: T[], filter: string, groups: NodeGroup[]): T[] {
  if (filter === 'all') return servers;
  if (filter === UNGROUPED_KEY) {
    const grouped = new Set(groups.flatMap((g) => g.members));
    return servers.filter((s) => !grouped.has(s.id));
  }
  const members = new Set(groups.find((g) => g.id === filter)?.members ?? []);
  return servers.filter((s) => members.has(s.id));
}

export interface GroupHealthRow {
  key: string;
  group: string;
  online: number;
  total: number;
}

/** Online/total per user group for a diagnosis; a node in several groups counts in each. */
export function groupHealthRows(results: FleetNodeResult[], groups: NodeGroup[]): GroupHealthRow[] {
  return bucketBy(results, (r) => r.id, 'groups', groups).map((b) => ({
    key: b.key,
    group: b.label,
    online: b.items.filter(isOnline).length,
    total: b.items.length,
  }));
}

export const OP_LABELS: Record<GroupOp, string> = {
  eq: tr.fleet.groupsLib.opLabels.eq,
  ne: tr.fleet.groupsLib.opLabels.ne,
  contains: tr.fleet.groupsLib.opLabels.contains,
  not_contains: tr.fleet.groupsLib.opLabels.not_contains,
  glob: tr.fleet.groupsLib.opLabels.glob,
};

export function conditionText(c: GroupCondition): string {
  const format = tr.fleet.groupsLib.condition[c.op];
  return format ? format(c.field, c.value) : tr.fleet.groupsLib.conditionFallback(c.field, c.op, c.value);
}

/** "tier = Tier-1 그리고 arch = aarch64 / name 패턴 site-b-*" */
export function rulesText(rules: NodeGroup['rules']): string {
  return rules.map((r) => r.conditions.map(conditionText).join(tr.fleet.groupsLib.and)).join(' / ');
}

export interface GroupReachability {
  /** False when there is no fleet diagnosis at all (nothing is known; never guessed). */
  probed: boolean;
  /** Members of the group. */
  total: number;
  /** Members the diagnosis reports ONLINE. */
  online: number;
  /** Names of members the diagnosis reports as not online. */
  offline: string[];
  /** Members the diagnosis has no result for. */
  notProbed: number;
}

/** Reachability of one group's members from the shared fleet diagnosis. */
export function groupReachability(group: NodeGroup, servers: FleetNode[], report: FleetDiagnosisReport | null): GroupReachability {
  if (!report) return { probed: false, total: group.members.length, online: 0, offline: [], notProbed: group.members.length };
  const results = new Map((report.results ?? []).map((r) => [r.id, r]));
  const names = new Map(servers.map((s) => [s.id, s.name]));
  let online = 0;
  let notProbed = 0;
  const offline: string[] = [];
  for (const id of group.members) {
    const r = results.get(id);
    if (!r) notProbed += 1;
    else if (isOnline(r)) online += 1;
    else offline.push(r.name || names.get(id) || id);
  }
  return { probed: true, total: group.members.length, online, offline, notProbed };
}

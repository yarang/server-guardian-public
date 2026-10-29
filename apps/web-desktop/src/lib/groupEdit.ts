import type { NodeGroup, NodeGroupData } from '../types';

/**
 * Pure helpers for editing group membership from Nodes and Node detail. They only rewrite the
 * `include` / `exclude` lists of a group definition; rules and member resolution stay with the hub.
 */

/** How a node is in a group: picked explicitly (`include`) or matched by a rule. */
export type MembershipOrigin = 'added' | 'rule';

/**
 * The editable definition of a group, as the hub accepts it. The hub rejects unknown keys, so the
 * resolved `members`, `warnings` and timestamps of a listed group are dropped.
 */
export function groupData(g: NodeGroup): NodeGroupData {
  return {
    id: g.id,
    name: g.name,
    description: g.description || undefined,
    tone: g.tone ?? undefined,
    include: [...g.include],
    rules: g.rules,
    exclude: [...g.exclude],
  };
}

/** Why `id` is a member: in `include` -> 'added'; otherwise a rule matched it. Null when it is not a member. */
export function membershipOrigin(g: NodeGroup, id: string): MembershipOrigin | null {
  if (!g.members.includes(id)) return null;
  return g.include.includes(id) ? 'added' : 'rule';
}

export interface AddPlan {
  /** The definition to save. */
  data: NodeGroupData;
  /** Ids newly put into `include`. */
  added: string[];
  /** Ids that were already in `include` (left as is). */
  unchanged: string[];
  /** Ids taken out of `exclude` so they are not removed again by the exclusion. */
  unexcluded: string[];
  /** False when saving would change nothing. */
  changes: boolean;
}

/** Adds `ids` to the group's `include` (deduplicated) and drops them from its `exclude`. */
export function planAdd(g: NodeGroup, ids: string[]): AddPlan {
  const wanted = Array.from(new Set(ids));
  const added = wanted.filter((id) => !g.include.includes(id));
  const unchanged = wanted.filter((id) => g.include.includes(id));
  const unexcluded = wanted.filter((id) => g.exclude.includes(id));
  const base = groupData(g);
  return {
    data: { ...base, include: [...g.include, ...added], exclude: g.exclude.filter((id) => !wanted.includes(id)) },
    added,
    unchanged,
    unexcluded,
    changes: added.length > 0 || unexcluded.length > 0,
  };
}

export interface RemovePlan {
  data: NodeGroupData;
  /** 'include': dropped from the picked nodes. 'exclude': added to the exclusions of a rule match. */
  how: 'include' | 'exclude';
}

/**
 * Takes a member out of a group: an explicit member leaves `include`; a rule-matched member is
 * added to `exclude` (the rules stay). Null when the node is not a member.
 */
export function planRemove(g: NodeGroup, id: string): RemovePlan | null {
  const origin = membershipOrigin(g, id);
  if (origin === null) return null;
  const base = groupData(g);
  if (origin === 'added') return { how: 'include', data: { ...base, include: g.include.filter((x) => x !== id) } };
  return { how: 'exclude', data: { ...base, exclude: g.exclude.includes(id) ? base.exclude : [...g.exclude, id] } };
}

import type { CharacterSeat, FlowData, FlowJob, FlowLink, FlowSeat, PersonaConfig } from '../types';

/**
 * Flow graphs (docs/design/flow-graph.md): the pure parts of the editor. A flow is seats (a character with a job),
 * bundles (seats that run in parallel), one close (the Judge) and links. `start`, `close` and `end` are fixed ids.
 * The hub (cli/llm-agent/flow_graph.py) stays the validator; `checkFlow` names the common problems on the canvas
 * before a round trip.
 */

export const START = 'start';
export const CLOSE = 'close';
export const END = 'end';
export const FIXED_IDS = [START, CLOSE, END] as const;
export const JOBS: FlowJob[] = ['write', 'critique', 'rewrite', 'rebut', 'summarise'];
export const SEAT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
export const FLOW_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
export const MAX_SEATS = 12;
export const MAX_BUNDLE_SEATS = 4;

/** The engine seats a job may use; a character (format 2) must list one of them (flow_graph.JOB_SEATS). */
export function jobSeats(job: FlowJob | 'close', mode?: string): CharacterSeat[] {
  switch (job) {
    case 'write': return ['expert', 'proposer', 'stage'];
    case 'rewrite': return ['stage', 'expert'];
    case 'rebut': return ['side'];
    case 'critique': return mode === 'checklist' ? ['verifier'] : ['critic'];
    case 'close': return ['judge'];
    default: return [];
  }
}

/** Whether a persona may take a job: a format 1 persona has no seat list and fits anywhere. */
export function characterFits(persona: PersonaConfig, job: FlowJob | 'close', mode?: string): boolean {
  if (persona.format !== 2 || !Array.isArray(persona.seats)) return true;
  const allowed = jobSeats(job, mode);
  return persona.seats.some((s) => allowed.includes(s));
}

/** The unit a seat is ordered with: its bundle, or itself. */
export function unitOf(flow: FlowData, id: string): string {
  return (flow.bundles ?? []).find((b) => b.seats.includes(id))?.id ?? id;
}

function forwardLinks(flow: FlowData): FlowLink[] {
  return flow.links.filter((l) => !l.look_back && l.from !== CLOSE);
}

function edgesOf(flow: FlowData): Map<string, Set<string>> {
  const edges = new Map<string, Set<string>>();
  for (const l of forwardLinks(flow)) {
    const a = unitOf(flow, l.from);
    const b = unitOf(flow, l.to);
    if (!edges.has(a)) edges.set(a, new Set());
    edges.get(a)!.add(b);
  }
  return edges;
}

function reach(edges: Map<string, Set<string>>, starts: string[]): Set<string> {
  const seen = new Set<string>();
  const todo = [...starts];
  while (todo.length) {
    const n = todo.pop()!;
    if (seen.has(n)) continue;
    seen.add(n);
    for (const m of edges.get(n) ?? []) todo.push(m);
  }
  return seen;
}

/** True when a forward link from `from` to `to` would close a loop of forward links. */
export function wouldLoop(flow: FlowData, from: string, to: string): boolean {
  const a = unitOf(flow, from);
  const b = unitOf(flow, to);
  return a === b || reach(edgesOf(flow), [b]).has(a);
}

/** Rank (column) of every unit: the longest forward path from the start; units the start never reaches follow the rest. */
export function ranks(flow: FlowData): Map<string, number> {
  const edges = edgesOf(flow);
  const rank = new Map<string, number>([[START, 0]]);
  const units = new Set<string>([START, CLOSE, END, ...flow.seats.map((s) => unitOf(flow, s.id)), ...(flow.bundles ?? []).map((b) => b.id)]);
  // longest path by relaxation; forward links are acyclic when the flow is valid, so |units| passes suffice
  for (let pass = 0; pass < units.size; pass++) {
    let changed = false;
    for (const [a, targets] of edges) {
      const ra = rank.get(a);
      if (ra === undefined) continue;
      for (const b of targets) {
        if ((rank.get(b) ?? -1) < ra + 1 && ra + 1 <= units.size) {
          rank.set(b, ra + 1);
          changed = true;
        }
      }
    }
    if (!changed) break;
  }
  const deepest = Math.max(0, ...[...rank.entries()].filter(([u]) => u !== CLOSE && u !== END).map(([, r]) => r));
  for (const u of units) {
    if (!rank.has(u) && u !== CLOSE && u !== END) rank.set(u, 1);   // run only after a rejection (a pipeline's lead)
  }
  const last = Math.max(deepest, ...[...rank.entries()].filter(([u]) => u !== CLOSE && u !== END).map(([, r]) => r));
  rank.set(CLOSE, Math.max(rank.get(CLOSE) ?? 0, last + 1));
  rank.set(END, (rank.get(CLOSE) ?? 0) + 1);
  return rank;
}

export const COLUMN = 260;
export const ROW = 120;
export const SEAT_W = 200;
export const SEAT_H = 76;
export const BUNDLE_PAD = 32;

/** Height of a bundle frame holding `n` seats. */
export function bundleHeight(n: number): number {
  return BUNDLE_PAD + n * (SEAT_H + 12) + 8;
}

/** Positions of the units (start, close, end, seats outside bundles, bundles): the stored layout, else by rank. */
export function autoLayout(flow: FlowData): Record<string, [number, number]> {
  const rank = ranks(flow);
  const byColumn = new Map<number, string[]>();
  for (const [u, r] of rank) {
    if (!byColumn.has(r)) byColumn.set(r, []);
    byColumn.get(r)!.push(u);
  }
  const out: Record<string, [number, number]> = {};
  for (const [r, units] of byColumn) {
    let y = 0;
    for (const u of units.sort()) {
      out[u] = [r * COLUMN, y];
      const bundle = (flow.bundles ?? []).find((b) => b.id === u);
      y += (bundle ? bundleHeight(bundle.seats.length) : SEAT_H) + ROW - SEAT_H + 24;
    }
  }
  return { ...out, ...(flow.layout ?? {}) };
}

/** A fresh id for a new seat: `<job>-<n>`, unused in the flow. */
export function newSeatId(flow: FlowData, job: FlowJob): string {
  const used = new Set([...flow.seats.map((s) => s.id), ...(flow.bundles ?? []).map((b) => b.id), ...FIXED_IDS]);
  let n = 1;
  while (used.has(`${job}-${n}`)) n++;
  return `${job}-${n}`;
}

/** A new flow: one writing lead and the close, linked so it already validates once characters are seated. */
export function blankFlow(id = '', name = ''): FlowData {
  return {
    id,
    name,
    category: 'general',
    seats: [{ id: 'lead', job: 'write', character: '', providers: [] }],
    close: { character: '', providers: [] },
    links: [
      { from: START, to: 'lead' },
      { from: 'lead', to: CLOSE },
      { from: CLOSE, to: 'lead', exit: 'rejected' },
      { from: CLOSE, to: END, exit: 'approved' },
    ],
  };
}

export function addSeat(flow: FlowData, job: FlowJob, at?: [number, number]): { flow: FlowData; id: string } {
  const id = newSeatId(flow, job);
  const seat: FlowSeat = job === 'summarise' ? { id, job, providers: [] } : { id, job, character: '', providers: [] };
  if (job === 'critique') seat.mode = 'object';
  if (job === 'rebut') seat.stance = '';
  const layout = at ? { ...(flow.layout ?? {}), [id]: at } : flow.layout;
  return { flow: { ...flow, seats: [...flow.seats, seat], ...(layout ? { layout } : {}) }, id };
}

/** Removes a seat (and its links and bundle membership; an emptied bundle goes too) or a bundle (its seats stay). */
export function removeElement(flow: FlowData, id: string): FlowData {
  const bundle = (flow.bundles ?? []).find((b) => b.id === id);
  if (bundle) {
    return {
      ...flow,
      bundles: (flow.bundles ?? []).filter((b) => b.id !== id),
      links: flow.links.filter((l) => l.from !== id && l.to !== id),
    };
  }
  const bundles = (flow.bundles ?? []).map((b) => ({ ...b, seats: b.seats.filter((s) => s !== id) }));
  const emptied = bundles.filter((b) => b.seats.length === 0).map((b) => b.id);
  const layout = { ...(flow.layout ?? {}) };
  delete layout[id];
  return {
    ...flow,
    seats: flow.seats.filter((s) => s.id !== id),
    bundles: bundles.filter((b) => b.seats.length > 0),
    links: flow.links.filter((l) => ![l.from, l.to].includes(id) && !emptied.some((e) => e === l.from || e === l.to)),
    layout,
  };
}

/** Puts a seat into a bundle (`new` makes one around it), or takes it out (`null`). */
export function setBundle(flow: FlowData, seatId: string, target: string | null | 'new'): FlowData {
  let bundles = (flow.bundles ?? []).map((b) => ({ ...b, seats: b.seats.filter((s) => s !== seatId) }));
  if (target === 'new') {
    const used = new Set([...flow.seats.map((s) => s.id), ...bundles.map((b) => b.id), ...FIXED_IDS]);
    let n = 1;
    while (used.has(`bundle-${n}`)) n++;
    bundles.push({ id: `bundle-${n}`, seats: [seatId] });
  } else if (target) {
    bundles = bundles.map((b) => (b.id === target ? { ...b, seats: [...b.seats, seatId] } : b));
  }
  const emptied = new Set(bundles.filter((b) => b.seats.length === 0).map((b) => b.id));
  return {
    ...flow,
    bundles: bundles.filter((b) => !emptied.has(b.id)),
    links: flow.links.filter((l) => !emptied.has(l.from) && !emptied.has(l.to)),
  };
}

/**
 * Adds a link `to` reads `from`. From the close it is the given exit (replacing the old one). A link that would close
 * a loop of forward links becomes a look-back link (it reads the previous round). Returns null for a link that
 * cannot exist (into the start, out of the end, a duplicate).
 */
export function addLink(flow: FlowData, from: string, to: string, exit?: 'approved' | 'rejected'): FlowData | null {
  if (from === to || to === START || from === END) return null;
  if (from === CLOSE) {
    const kind = exit ?? (to === END ? 'approved' : 'rejected');
    if ((kind === 'approved') !== (to === END)) return null;
    return { ...flow, links: [...flow.links.filter((l) => !(l.from === CLOSE && l.exit === kind)), { from, to, exit: kind }] };
  }
  if (to === END) return null;
  if (flow.links.some((l) => l.from === from && l.to === to && !l.exit)) return null;
  const link: FlowLink = { from, to };
  if (from !== START && wouldLoop(flow, from, to)) {
    if (unitOf(flow, from) === unitOf(flow, to)) return null;   // members of one bundle do not read each other
    link.look_back = true;
  }
  return { ...flow, links: [...flow.links, link] };
}

export function linkKey(l: FlowLink): string {
  return `${l.from}->${l.to}${l.exit ? `:${l.exit}` : ''}`;
}

export function updateLink(flow: FlowData, key: string, change: Partial<FlowLink> | null): FlowData {
  const links = change === null
    ? flow.links.filter((l) => linkKey(l) !== key)
    : flow.links.map((l) => (linkKey(l) === key ? Object.fromEntries(Object.entries({ ...l, ...change }).filter(([, v]) => v !== undefined && v !== false)) as unknown as FlowLink : l));
  return { ...flow, links };
}

export function updateSeat(flow: FlowData, id: string, change: Partial<FlowSeat>): FlowData {
  return { ...flow, seats: flow.seats.map((s) => (s.id === id ? clean({ ...s, ...change }) : s)) };
}

/** Drops empty optional fields so the saved file stays short and the hub sees no meaningless keys. */
function clean(seat: FlowSeat): FlowSeat {
  const out: Record<string, unknown> = { ...seat };
  if (seat.job !== 'critique') delete out.mode;
  if (seat.job !== 'rebut') delete out.stance;
  if (seat.job !== 'summarise') delete out.instruction;
  for (const key of ['input', 'history']) if (out[key] === 'raw' || out[key] === 'digest' || out[key] === '') delete out[key];
  for (const key of ['code_tools', 'host', 'focus', 'instruction']) if (out[key] === '' || out[key] === null) delete out[key];
  if (seat.job === 'summarise' && !out.character) delete out.character;
  return out as unknown as FlowSeat;
}

/** Model calls of a first round (flow_graph.calls_per_round): every seat, two more per rebut answer, the close. */
export function callsPerRound(flow: FlowData): number {
  const extra = (flow.bundles ?? []).reduce((n, b) => {
    const rebut = b.seats.some((id) => flow.seats.find((s) => s.id === id)?.job === 'rebut');
    return n + (rebut ? 2 * (b.exchanges ?? 1) : 0);
  }, 0);
  return flow.seats.length + extra + 1;
}

/** What a seat (or the close) reads: the elements linked into it or into its bundle, and how. */
export function inputsOf(flow: FlowData, id: string): Array<{ from: string; lookBack: boolean; blocked: boolean }> {
  const bundle = unitOf(flow, id);
  return flow.links
    .filter((l) => !l.exit && (l.to === id || (bundle !== id && l.to === bundle)))
    .map((l) => ({ from: l.from, lookBack: !!l.look_back, blocked: !!l.blocked }));
}

/** A problem the canvas marks: on an element (`target` = seat, bundle, `close` or a link key) or on the flow. */
export type FlowIssue =
  | { code: 'noCharacter'; target: string }
  | { code: 'characterUnfit'; target: string; character: string }
  | { code: 'unknownCharacter'; target: string; character: string }
  | { code: 'noExit'; target: 'close'; exit: 'approved' | 'rejected' }
  | { code: 'neverRuns'; target: string }
  | { code: 'deadEnd'; target: string }
  | { code: 'noPath'; target: 'flow' }
  | { code: 'summariserFeeds'; target: string }
  | { code: 'condensedNeedsSummariser'; target: string }
  | { code: 'rebutPair'; target: string }
  | { code: 'noStance'; target: string }
  | { code: 'noWriter'; target: 'flow' }
  | { code: 'tooManySeats'; target: 'flow'; max: number }
  | { code: 'badId'; target: 'flow' }
  | { code: 'noName'; target: 'flow' };

/** The common problems, in the order of flow_graph.validate_flow; the hub's answer stays the authority. */
export function checkFlow(flow: FlowData, personas?: PersonaConfig[]): FlowIssue[] {
  const issues: FlowIssue[] = [];
  if (!FLOW_ID_RE.test(flow.id)) issues.push({ code: 'badId', target: 'flow' });
  if (!flow.name.trim()) issues.push({ code: 'noName', target: 'flow' });
  if (flow.seats.length > MAX_SEATS) issues.push({ code: 'tooManySeats', target: 'flow', max: MAX_SEATS });
  if (!flow.seats.some((s) => ['write', 'rewrite', 'rebut'].includes(s.job))) issues.push({ code: 'noWriter', target: 'flow' });
  const byId = new Map((personas ?? []).map((p) => [p.id, p]));
  const characterIssue = (target: string, character: string | undefined, job: FlowJob | 'close', mode?: string) => {
    if (!character) return issues.push({ code: 'noCharacter', target });
    if (!personas) return;
    const persona = byId.get(character);
    if (!persona) issues.push({ code: 'unknownCharacter', target, character });
    else if (!characterFits(persona, job, mode)) issues.push({ code: 'characterUnfit', target, character });
  };
  for (const seat of flow.seats) {
    if (seat.job !== 'summarise') characterIssue(seat.id, seat.character, seat.job, seat.mode);
    if (seat.job === 'rebut' && !seat.stance?.trim()) issues.push({ code: 'noStance', target: seat.id });
  }
  characterIssue(CLOSE, flow.close.character, 'close');
  for (const exit of ['approved', 'rejected'] as const) {
    if (!flow.links.some((l) => l.from === CLOSE && l.exit === exit)) issues.push({ code: 'noExit', target: 'close', exit });
  }
  for (const seat of flow.seats.filter((s) => s.job === 'rebut')) {
    const bundle = (flow.bundles ?? []).find((b) => b.seats.includes(seat.id));
    const pair = bundle && bundle.seats.length === 2 && bundle.seats.every((id) => flow.seats.find((s) => s.id === id)?.job === 'rebut');
    if (!pair) issues.push({ code: 'rebutPair', target: seat.id });
  }
  const edges = edgesOf(flow);
  const rejected = flow.links.find((l) => l.from === CLOSE && l.exit === 'rejected');
  const runs = new Set([...reach(edges, [START]), ...(rejected ? reach(edges, [unitOf(flow, rejected.to)]) : [])]);
  if (!reach(edges, [START]).has(CLOSE)) issues.push({ code: 'noPath', target: 'flow' });
  for (const seat of flow.seats) {
    const unit = unitOf(flow, seat.id);
    if (!runs.has(unit)) issues.push({ code: 'neverRuns', target: seat.id });
    else if (!reach(edges, [unit]).has(CLOSE)) issues.push({ code: 'deadEnd', target: seat.id });
  }
  const forward = forwardLinks(flow);
  for (const seat of flow.seats.filter((s) => s.job === 'summarise')) {
    if (forward.filter((l) => l.from === seat.id).length !== 1) issues.push({ code: 'summariserFeeds', target: seat.id });
  }
  const condensed = [...flow.seats.filter((s) => s.input === 'condensed').map((s) => s.id), ...(flow.close.input === 'condensed' ? [CLOSE] : [])];
  for (const id of condensed) {
    const readers = forward.filter((l) => l.to === id).map((l) => flow.seats.find((s) => s.id === l.from));
    if (!readers.some((s) => s?.job === 'summarise')) issues.push({ code: 'condensedNeedsSummariser', target: id });
  }
  return issues;
}

/** The flow as saved: positions of the canvas, no `source`, empty bundles dropped. */
export function forSave(flow: FlowData, positions: Record<string, [number, number]>): FlowData {
  const out: FlowData = { ...flow, layout: positions };
  delete (out as Record<string, unknown>).source;
  if (!out.bundles?.length) delete out.bundles;
  if (out.params && Object.keys(out.params).length === 0) delete out.params;
  if (!out.params && !out.task?.trim()) delete out.task;
  if (out.projects && out.projects.length === 0) delete out.projects;
  return out;
}

/** The flows the start form offers in a project of this kind: valid, runnable, and made for that kind (or any). */
export function startableFlows<T extends { errors: string[]; runnable: boolean; data: FlowData }>(entries: T[] | undefined, projectKind: 'code' | 'ops' | 'design' | undefined): T[] {
  const kind = projectKind ?? 'code';
  return (entries ?? []).filter((e) => e.errors.length === 0 && e.runnable && (!e.data.projects?.length || e.data.projects.includes(kind)));
}

/** D8: the parameter types the editor offers. Text picks `string` (one line) up to 200 characters, `text` above. */
export const TEXT_ONE_LINE_MAX = 200;
export function textParamType(max: number): 'string' | 'text' {
  return max <= TEXT_ONE_LINE_MAX ? 'string' : 'text';
}

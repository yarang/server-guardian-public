import { describe, expect, it } from 'vitest';
import {
  addLink,
  addSeat,
  autoLayout,
  blankFlow,
  callsPerRound,
  characterFits,
  checkFlow,
  forSave,
  inputsOf,
  ranks,
  removeElement,
  setBundle,
  startableFlows,
  textParamType,
  updateLink,
  updateSeat,
  wouldLoop,
} from '../src/lib/flowGraph';
import type { FlowData, FlowEntry, PersonaConfig } from '../src/types';

/** The debate shape: lead -> critics bundle -> close, critics read back to the lead. */
function debate(): FlowData {
  return {
    id: 'review',
    name: 'Review',
    seats: [
      { id: 'lead', job: 'write', character: 'architect' },
      { id: 'sec', job: 'critique', character: 'redteam' },
      { id: 'qa', job: 'critique', character: 'qa' },
    ],
    bundles: [{ id: 'critics', seats: ['sec', 'qa'] }],
    close: { character: 'judge' },
    links: [
      { from: 'start', to: 'lead' },
      { from: 'lead', to: 'critics' },
      { from: 'critics', to: 'lead', look_back: true },
      { from: 'critics', to: 'close' },
      { from: 'close', to: 'lead', exit: 'rejected', max_rounds: 2 },
      { from: 'close', to: 'end', exit: 'approved' },
    ],
  };
}

const personas: PersonaConfig[] = [
  { id: 'architect', name: 'Architect' },
  { id: 'redteam', name: 'Red team' },
  { id: 'qa', name: 'QA' },
  { id: 'judge', name: 'Judge' },
  { id: 'only_critic', name: 'Only critic', format: 2, seats: ['critic'] },
];

describe('flow graph library', () => {
  it('a valid debate has no issues and the calls of a round', () => {
    expect(checkFlow(debate(), personas)).toEqual([]);
    expect(callsPerRound(debate())).toBe(4);
  });

  it('ranks units by the longest forward path and lays them out in columns', () => {
    const r = ranks(debate());
    expect([r.get('start'), r.get('lead'), r.get('critics'), r.get('close'), r.get('end')]).toEqual([0, 1, 2, 3, 4]);
    const layout = autoLayout(debate());
    expect(layout.critics[0]).toBeGreaterThan(layout.lead[0]);
    expect(autoLayout({ ...debate(), layout: { lead: [5, 6] } }).lead).toEqual([5, 6]);
  });

  it('a backward link becomes a look-back link; members of one bundle do not read each other', () => {
    const flow = debate();
    expect(wouldLoop(flow, 'critics', 'lead')).toBe(true);
    const added = addLink({ ...flow, links: flow.links.filter((l) => !l.look_back) }, 'qa', 'lead');
    expect(added?.links.at(-1)).toEqual({ from: 'qa', to: 'lead', look_back: true });
    expect(addLink(flow, 'sec', 'qa')).toBeNull();
    expect(addLink(flow, 'lead', 'start')).toBeNull();
    expect(addLink(flow, 'lead', 'end')).toBeNull();
  });

  it('the close has one link per exit, replaced when drawn again', () => {
    const flow = addLink(debate(), 'close', 'critics', 'rejected')!;
    expect(flow.links.filter((l) => l.exit === 'rejected')).toEqual([{ from: 'close', to: 'critics', exit: 'rejected' }]);
    expect(addLink(debate(), 'close', 'lead', 'approved')).toBeNull();
  });

  it('names the problems on their elements', () => {
    const flow = debate();
    flow.seats[1].character = '';
    flow.seats[0].character = 'only_critic';
    flow.links = flow.links.filter((l) => l.exit !== 'approved');
    flow.seats.push({ id: 'lonely', job: 'critique', character: 'qa' });
    const codes = checkFlow(flow, personas).map((i) => `${i.code}:${i.target}`);
    expect(codes).toEqual(expect.arrayContaining(['noCharacter:sec', 'characterUnfit:lead', 'noExit:close', 'neverRuns:lonely']));
  });

  it('condensed input needs a summariser, and a summariser feeds one seat', () => {
    const flow = debate();
    flow.close.input = 'condensed';
    expect(checkFlow(flow, personas).map((i) => i.code)).toContain('condensedNeedsSummariser');
    let next = addSeat(flow, 'summarise').flow;
    next = { ...next, links: [...next.links.filter((l) => !(l.from === 'critics' && l.to === 'close')), { from: 'critics', to: 'summarise-1' }, { from: 'summarise-1', to: 'close' }] };
    expect(checkFlow(next, personas)).toEqual([]);
    expect(inputsOf(next, 'close')).toEqual([{ from: 'summarise-1', lookBack: false, blocked: false }]);
  });

  it('bundles: join, new, leave; an emptied bundle and its links go', () => {
    let flow = setBundle(debate(), 'lead', 'new');
    expect(flow.bundles?.map((b) => b.id)).toEqual(['critics', 'bundle-1']);
    flow = setBundle(flow, 'lead', null);
    expect(flow.bundles?.map((b) => b.id)).toEqual(['critics']);
    flow = removeElement(removeElement(debate(), 'sec'), 'qa');
    expect(flow.bundles).toEqual([]);
    expect(flow.links.some((l) => l.from === 'critics' || l.to === 'critics')).toBe(false);
  });

  it('edits drop empty optional fields', () => {
    const flow = updateSeat(debate(), 'lead', { input: 'raw', stance: 'x', host: '' });
    expect(flow.seats[0]).toEqual({ id: 'lead', job: 'write', character: 'architect' });
    const linked = updateLink(debate(), 'critics->lead', { look_back: false });
    expect(linked.links.find((l) => l.from === 'critics' && l.to === 'lead')).toEqual({ from: 'critics', to: 'lead' });
  });

  it('characters fit by their seats; format 1 personas fit anywhere', () => {
    expect(characterFits(personas[4], 'critique')).toBe(true);
    expect(characterFits(personas[4], 'critique', 'checklist')).toBe(false);
    expect(characterFits(personas[4], 'write')).toBe(false);
    expect(characterFits(personas[0], 'close')).toBe(true);
  });

  it('save form, startable flows and the D8 text type', () => {
    const saved = forSave({ ...blankFlow('x', 'X'), params: {}, task: '  ', projects: [] }, { lead: [1, 2] });
    expect(saved).not.toHaveProperty('params');
    expect(saved).not.toHaveProperty('task');
    expect(saved).not.toHaveProperty('projects');
    expect(saved.layout).toEqual({ lead: [1, 2] });
    const entry = (over: Partial<FlowEntry>): FlowEntry => ({ id: 'a', name: 'a', source: 'custom', data: debate(), hash: 'h', calls: 4, runnable: true, run_error: null, errors: [], warnings: [], ...over });
    const list = [entry({ id: 'any' }), entry({ id: 'ops', data: { ...debate(), projects: ['ops'] } }), entry({ id: 'later', runnable: false }), entry({ id: 'bad', errors: ['x'] })];
    expect(startableFlows(list, undefined).map((e) => e.id)).toEqual(['any']);
    expect(startableFlows(list, 'ops').map((e) => e.id)).toEqual(['any', 'ops']);
    expect([textParamType(200), textParamType(201)]).toEqual(['string', 'text']);
  });
});

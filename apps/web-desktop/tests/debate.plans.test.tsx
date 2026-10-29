import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { en } from '../src/i18n/en';
import { ko } from '../src/i18n/ko';
import { action } from './debateFixtures';

const P = ko.debate.plans;

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubError } from '../src/api';
import { PLAN_ID_RE, planState } from '../src/lib/debate';
import { ActionDialog } from '../src/pages/debate/ActionDialog';
import { PlanDialog } from '../src/pages/debate/PlanDialog';
import { PlansPanel } from '../src/pages/debate/PlansPanel';
import { OperationsStoreProvider } from '../src/state/operationsStore';
import type { ActionItem, PlanItem } from '../src/types';

const mocked = vi.mocked(api);
const flush = () => act(async () => undefined);

function step(id: number, name: string, over: Partial<ActionItem> = {}): ActionItem {
  return action({
    id,
    tool: 'ops:package_install',
    arguments: { target_host: 'node-1', packages: [{ name: 'jq' }] },
    reason: `step ${name}`,
    plan_id: 'plan_a_r1',
    step: name,
    requires: [],
    on_failure: 'stop',
    risk: 'medium',
    preview: { bytes: 12, text: 'jq 1.7 (new)' },
    ...over,
  });
}

function plan(over: Partial<PlanItem> = {}, steps: ActionItem[] = [step(1, 's1'), step(2, 's2', { requires: ['s1'] })]): PlanItem {
  const counts: Record<string, number> = {};
  for (const s of steps) counts[String(s.status)] = (counts[String(s.status)] ?? 0) + 1;
  return {
    id: 'plan_a_r1',
    thread_id: 'thread_a',
    goal: 'Install jq on node-1',
    stop_plan: true,
    steps,
    hosts: [{ host: 'node-1', steps: steps.map((s) => s.step as string) }],
    counts,
    risk: 'medium',
    runnable: true,
    ...over,
  };
}

const onClose = vi.fn();
const onChanged = vi.fn();
const dialog = (id = 'plan_a_r1') => screen.getByRole('dialog', { name: P.dialogTitle(id) });

async function open(p: PlanItem) {
  mocked.getPlan.mockResolvedValue(p);
  render(
    <GuardianRoot>
      <PlanDialog id={p.id} onClose={onClose} onChanged={onChanged} />
    </GuardianRoot>
  );
  await screen.findByRole('dialog', { name: P.dialogTitle(p.id) });
}

beforeEach(() => {
  for (const fn of [mocked.getPlan, mocked.runPlan, mocked.rejectPlan, mocked.listPlans, mocked.getAction]) fn.mockReset();
  onClose.mockReset();
  onChanged.mockReset();
});
afterEach(() => vi.useRealTimers());

describe('plan helpers', () => {
  it('derives the plan state from the step counts', () => {
    expect(planState({ counts: { RUNNING: 1, PROPOSED: 2 } })).toBe('running');
    expect(planState({ counts: { PROPOSED: 1, EXECUTED: 1 } })).toBe('awaiting');
    expect(planState({ counts: { HELD: 1, EXECUTED: 1 } })).toBe('held');
    expect(planState({ counts: { EXECUTED: 1, FAILED: 1, SKIPPED: 1 } })).toBe('failed');
    expect(planState({ counts: { EXECUTED: 2 } })).toBe('done');
    expect(planState({ counts: { REJECTED: 2 } })).toBe('rejected');
  });

  it('accepts only plan ids', () => {
    expect(PLAN_ID_RE.test('plan_thread_a_r1')).toBe(true);
    expect(PLAN_ID_RE.test('57')).toBe(false);
    expect(PLAN_ID_RE.test('plan_../x')).toBe(false);
  });

  it('keeps the plans catalog the same shape in ko and en', () => {
    expect(Object.keys(en.debate.plans).sort()).toEqual(Object.keys(ko.debate.plans).sort());
    expect(Object.keys(en.debate.actionStatus)).toContain('SKIPPED');
    expect(Object.keys(ko.debate.actionStatus)).toContain('SKIPPED');
  });
});

describe('PlansPanel', () => {
  const renderPanel = (onOpen = vi.fn()) =>
    render(
      <GuardianRoot>
        <PlansPanel version={0} project="fleet" onOpen={onOpen} />
      </GuardianRoot>
    );

  it('renders nothing when the project has no plans', async () => {
    mocked.listPlans.mockResolvedValue([]);
    const { container } = renderPanel();
    await flush();
    expect(mocked.listPlans).toHaveBeenCalledWith(undefined, 'fleet');
    expect(container.querySelector('table')).toBeNull();
  });

  it('lists plans and opens one by its id', async () => {
    mocked.listPlans.mockResolvedValue([plan()]);
    const onOpen = vi.fn();
    renderPanel(onOpen);
    const table = await screen.findByRole('table', { name: P.title });
    expect(within(table).getByText('Install jq on node-1')).toBeTruthy();
    expect(within(table).getByText('node-1')).toBeTruthy();
    expect(within(table).getByText(P.state.awaiting)).toBeTruthy();
    fireEvent.click(within(table).getByRole('button', { name: P.dialogTitle('plan_a_r1') }));
    expect(onOpen).toHaveBeenCalledWith('plan_a_r1');
  });
});

describe('PlanDialog', () => {
  it('shows the goal, the steps per node, the simulation and the stop-plan note', async () => {
    await open(plan());
    const d = dialog();
    expect(within(d).getByText('Install jq on node-1')).toBeTruthy();
    expect(within(d).getByText(P.nodeSteps('node-1', 2))).toBeTruthy();
    expect(within(d).getAllByText('jq 1.7 (new)').length).toBeGreaterThan(0);
    expect(within(d).getByText(P.stopPlanNote)).toBeTruthy();
    expect(within(d).getByText('s2')).toBeTruthy();
  });

  it('offers to save an ad-hoc script as a command set, prefilled and not saved until confirmed', async () => {
    mocked.saveCommandSet.mockResolvedValue({ id: 'rotate-tmp', warnings: [] });
    mocked.fetchCommandSets.mockResolvedValue({ ok: true, max_age_days: 180, stale: [], sets: [{ id: 'taken-one' } as never] });
    const script = step(1, 's1', {
      tool: 'ops:run_script', arguments: { target_host: 'node-1', script: 'find /var/tmp -mtime +7 -print', reason: 'list old temp files' },
    });
    await open(plan({}, [script, step(2, 's2')]));
    const C = ko.debate.commandSet;
    expect(within(dialog()).getAllByRole('button', { name: C.saveAsSet })).toHaveLength(1);
    fireEvent.click(within(dialog()).getByRole('button', { name: C.saveAsSet }));
    await flush();
    const form = screen.getByRole('dialog', { name: C.newTitle });
    expect((within(form).getByLabelText(C.script) as HTMLTextAreaElement).value).toBe('find /var/tmp -mtime +7 -print');
    expect((within(form).getByLabelText(C.description) as HTMLInputElement).value).toBe('list old temp files');
    expect(mocked.saveCommandSet).not.toHaveBeenCalled();
    // the id of a set that exists is refused on the spot, so the dialog cannot replace it by accident
    fireEvent.change(within(form).getByLabelText(C.id), { target: { value: 'taken-one' } });
    fireEvent.click(within(form).getByRole('button', { name: C.save }));
    await flush();
    expect(within(form).getByText(C.idTaken('taken-one'))).toBeTruthy();
    expect(mocked.saveCommandSet).not.toHaveBeenCalled();
    // Escape closes only the inner dialog, not the plan underneath
    fireEvent.keyDown(within(form).getByLabelText(C.script), { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: C.newTitle })).toBeNull();
    expect(within(dialog()).getAllByRole('button', { name: C.saveAsSet })).toHaveLength(1);
    fireEvent.click(within(dialog()).getByRole('button', { name: C.saveAsSet }));
    await flush();
    const form2 = screen.getByRole('dialog', { name: C.newTitle });
    fireEvent.change(within(form2).getByLabelText(C.id), { target: { value: 'rotate-tmp' } });
    fireEvent.click(within(form2).getByRole('button', { name: C.save }));
    await flush();
    expect(mocked.saveCommandSet.mock.calls[0][0]).toMatchObject({ id: 'rotate-tmp', script: 'find /var/tmp -mtime +7 -print' });
    expect(within(dialog()).getByText(C.saved('rotate-tmp'))).toBeTruthy();
  });

  it('warns when the node hides the owner of a listening port and lists what is protected', async () => {
    const preview = { bytes: 0, text: 'Remv htop', protected: ['nginx* (role web)'], blind: ['no owner found for listening port(s) 0.0.0.0:22'] };
    await open(plan({}, [step(1, 's1', { preview })]));
    const d = dialog();
    expect(within(d).getByText(ko.debate.details.blindSpots, { exact: false })).toBeTruthy();
    expect(within(d).getByText('no owner found for listening port(s) 0.0.0.0:22')).toBeTruthy();
    expect(within(d).getByText(ko.debate.details.protectedHere('nginx* (role web)'))).toBeTruthy();
  });

  it('shows no protection notes when the preview carries none', async () => {
    await open(plan());
    expect(within(dialog()).queryByText(ko.debate.details.blindSpots, { exact: false })).toBeNull();
  });

  it('says a deferred step is measured later', async () => {
    await open(plan({}, [step(1, 's1'), step(2, 's2', { requires: ['s1'], preview: null })]));
    expect(within(dialog()).getAllByText(P.deferredPreview).length).toBe(1);
  });

  it('medium risk: Run needs the checkbox, then posts the plan id and starts polling', async () => {
    mocked.runPlan.mockResolvedValue({ started: true, plan_id: 'plan_a_r1' });
    await open(plan());
    fireEvent.click(within(dialog()).getByRole('button', { name: P.run }));
    const confirm = screen.getByRole('dialog', { name: P.runQ('plan_a_r1') });
    const go = within(confirm).getByRole('button', { name: P.runNow }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.click(within(confirm).getByLabelText(P.reviewed));
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await flush();
    expect(mocked.runPlan).toHaveBeenCalledWith('plan_a_r1');
    expect(within(dialog()).getByText(P.started)).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('high risk: Run needs the exact plan id typed', async () => {
    mocked.runPlan.mockResolvedValue({ started: true, plan_id: 'plan_a_r1' });
    await open(plan({ risk: 'high' }));
    fireEvent.click(within(dialog()).getByRole('button', { name: P.run }));
    const confirm = screen.getByRole('alertdialog', { name: P.runQ('plan_a_r1') });
    const go = within(confirm).getByRole('button', { name: P.runNow }) as HTMLButtonElement;
    const typed = within(confirm).getByRole('textbox');
    expect(go.disabled).toBe(true);
    fireEvent.change(typed, { target: { value: 'plan_a_r' } });
    expect(go.disabled).toBe(true);
    fireEvent.change(typed, { target: { value: 'plan_a_r1' } });
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await flush();
    expect(mocked.runPlan).toHaveBeenCalledWith('plan_a_r1');
  });

  it('polls while the plan runs and reports the outcome once it ends', async () => {
    vi.useFakeTimers();
    const running = plan({ runnable: false }, [step(1, 's1', { status: 'RUNNING' }), step(2, 's2', { requires: ['s1'] })]);
    const finished = plan({ runnable: false }, [step(1, 's1', { status: 'EXECUTED' }), step(2, 's2', { requires: ['s1'], status: 'EXECUTED' })]);
    mocked.getPlan.mockResolvedValue(running);
    render(
      <GuardianRoot>
        <PlanDialog id="plan_a_r1" onClose={onClose} onChanged={onChanged} />
      </GuardianRoot>
    );
    await flush();
    expect(within(dialog()).getByText(P.polling)).toBeTruthy();
    mocked.getPlan.mockResolvedValue(finished);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    expect(within(dialog()).getByText(P.finished(P.state.done))).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
    expect(within(dialog()).queryByText(P.polling)).toBeNull();
  });

  it('a 409 on run explains why and reloads the plan', async () => {
    mocked.runPlan.mockRejectedValue(new HubError('plan is already running', 409));
    await open(plan());
    fireEvent.click(within(dialog()).getByRole('button', { name: P.run }));
    fireEvent.click(within(screen.getByRole('dialog', { name: P.runQ('plan_a_r1') })).getByLabelText(P.reviewed));
    fireEvent.click(within(screen.getByRole('dialog', { name: P.runQ('plan_a_r1') })).getByRole('button', { name: P.runNow }));
    await flush();
    expect(within(dialog()).getByText((t) => t.includes(P.notRunnable))).toBeTruthy();
    expect(mocked.getPlan.mock.calls.length).toBeGreaterThan(1);
  });

  it('rejects the waiting steps after a confirmation', async () => {
    const rejected = plan({ runnable: false }, [step(1, 's1', { status: 'REJECTED' }), step(2, 's2', { status: 'REJECTED' })]);
    mocked.rejectPlan.mockResolvedValue(rejected);
    await open(plan());
    fireEvent.click(within(dialog()).getByRole('button', { name: P.reject }));
    const confirm = screen.getByRole('dialog', { name: P.rejectQ('plan_a_r1') });
    expect(within(confirm).getByText(P.rejectMsg(2))).toBeTruthy();
    fireEvent.click(within(confirm).getByRole('button', { name: P.reject }));
    await flush();
    expect(mocked.rejectPlan).toHaveBeenCalledWith('plan_a_r1');
    expect(within(dialog()).getByText(P.rejected(2))).toBeTruthy();
    expect(within(dialog()).queryByRole('button', { name: P.run })).toBeNull();
  });

  it('hides Run and Reject once nothing is left to decide', async () => {
    await open(plan({ runnable: false }, [step(1, 's1', { status: 'EXECUTED' }), step(2, 's2', { status: 'FAILED' }), step(3, 's3', { status: 'SKIPPED' })]));
    const d = dialog();
    expect(within(d).queryByRole('button', { name: P.run })).toBeNull();
    expect(within(d).queryByRole('button', { name: P.reject })).toBeNull();
    expect(within(d).getByText(P.state.failed)).toBeTruthy();
    expect(within(d).getByText(ko.debate.actionStatus.SKIPPED)).toBeTruthy();
  });

  it('shows a load failure instead of an empty dialog', async () => {
    mocked.getPlan.mockRejectedValue(new HubError('plan not found', 404));
    render(
      <GuardianRoot>
        <PlanDialog id="plan_a_r1" onClose={onClose} onChanged={onChanged} />
      </GuardianRoot>
    );
    await flush();
    expect(within(dialog()).getByText(/plan not found/)).toBeTruthy();
  });
});

describe('PlanDialog: critic votes and the gate', () => {
  const vote = (over: Partial<NonNullable<PlanItem['votes']>[number]> = {}) => ({
    critic: 'ops_dependency_critic',
    vote: 'approve' as const,
    reason: 'nothing else needs jq',
    cites: [],
    counted: false,
    ...over,
  });
  const gate = (over: Partial<NonNullable<PlanItem['gate']>> = {}) => ({
    auto: false,
    would_auto: false,
    reasons: ['policy.batch.auto is off'],
    notes: [],
    tally: { approve: 1, oppose: 0, abstain: 0, objections: 0 },
    ...over,
  });

  it('shows nothing for a plan that had no vote pass', async () => {
    await open(plan());
    expect(within(dialog()).queryByText(P.gateTitle)).toBeNull();
  });

  it('says why a plan waits for a person and what each critic voted', async () => {
    await open(
      plan({
        plan_hash: 'abc123def456',
        votes: [
          vote(),
          vote({ critic: 'ops_provision_safety_critic', vote: 'oppose', counted: true, reason: 'disk is 97 % full', cites: ['[#2 disk_usage node-1]'] }),
          vote({ critic: 'ops_x', vote: 'oppose', counted: false, reason: 'feels risky' }),
          vote({ critic: 'ops_y', vote: 'abstain', reason: '', note: 'no valid vote: no JSON' }),
        ],
        gate: gate({ reasons: ['objection from ops_provision_safety_critic: disk is 97 % full'] }),
      })
    );
    const d = within(dialog());
    expect(d.getByText(P.gateWaiting)).toBeTruthy();
    expect(d.getByText('objection from ops_provision_safety_critic: disk is 97 % full')).toBeTruthy();
    expect(d.getByText(P.voteApprove)).toBeTruthy();
    expect(d.getByText(P.voteObjection)).toBeTruthy();
    expect(d.getByText(P.voteAdvice)).toBeTruthy();
    expect(d.getByText(P.voteAbstain)).toBeTruthy();
    expect(d.getByText('[#2 disk_usage node-1]')).toBeTruthy();
    expect(d.getByText('abc123def456')).toBeTruthy();
  });

  it('mentions the shadow decision when only the opt-in is missing', async () => {
    await open(plan({ votes: [vote()], gate: gate({ would_auto: true }) }));
    expect(within(dialog()).getByText(P.gateShadow)).toBeTruthy();
  });

  it('shows the reasons the start check refused a released plan', async () => {
    await open(
      plan({ votes: [vote()], gate: gate({ auto: true, would_auto: true, reasons: [], release_refused: { reasons: ['the plan changed after the critics voted on it'] } }) })
    );
    const d = within(dialog());
    expect(d.getByText(P.gateRefused)).toBeTruthy();
    expect(d.getByText('the plan changed after the critics voted on it')).toBeTruthy();
  });

  it('stops saying a plan waits once nothing in it is left to decide', async () => {
    await open(
      plan({ votes: [vote()], gate: gate({ auto: true, reasons: [], release_refused: { reasons: ['the plan changed'] } }), counts: { SUCCESS: 2 } }, [step(1, 's1', { status: 'SUCCESS' }), step(2, 's2', { status: 'SUCCESS' })])
    );
    const d = within(dialog());
    expect(d.queryByText(P.gateRefused)).toBeNull();
    expect(d.queryByText(P.gateWaiting)).toBeNull();
    expect(d.getByText(P.gateVotes(1))).toBeTruthy();
  });

  it('reports a plan the engine ran by itself, with its error if the run broke off', async () => {
    await open(
      plan({
        votes: [vote()],
        gate: gate({ auto: true, auto_run: { actor: 'autoplan:fleet', started_at: '2026-10-20T01:00:00Z', finished_at: '2026-10-20T01:01:00Z', error: 'RuntimeError: boom' } }),
      })
    );
    const d = within(dialog());
    expect(d.getByText(P.gateAutoRan('autoplan:fleet'))).toBeTruthy();
    expect(d.getByText(P.gateAutoError('RuntimeError: boom'))).toBeTruthy();
  });

  it('marks which steps can be undone', async () => {
    await open(plan({}, [step(1, 's1', { reversible: true }), step(2, 's2', { reversible: false })]));
    const d = within(dialog());
    expect(d.getAllByText(P.undoable).length).toBe(1);
    expect(d.getAllByText(P.irreversible).length).toBe(1);
  });
});

describe('ActionDialog for a plan step', () => {
  it('cannot run on its own and links to the plan', async () => {
    const s = step(7, 's1');
    mocked.getAction.mockResolvedValue(s);
    render(
      <GuardianRoot>
        <OperationsStoreProvider>
          <ActionDialog id={7} onClose={onClose} onChanged={onChanged} />
        </OperationsStoreProvider>
      </GuardianRoot>
    );
    const d = await screen.findByRole('dialog', { name: ko.debate.dialog.title(7) });
    expect(within(d).getByText(new RegExp('plan_a_r1'))).toBeTruthy();
    expect(within(d).queryByRole('button', { name: ko.debate.dialog.execute })).toBeNull();
    fireEvent.click(within(d).getByRole('button', { name: P.openPlan }));
    expect(window.location.hash).toBe('#/approvals/plan_a_r1');
  });
});

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { runningJob, teamList } from './debateFixtures';
import type { FlowEntry, FlowList } from '../src/types';
import { ko } from '../src/i18n/ko';

const D = ko.debate;
const F = ko.flows;

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const flow = (over: Partial<FlowEntry['data']> = {}, entry: Partial<FlowEntry> = {}): FlowEntry => {
  const data: FlowEntry['data'] = {
    id: 'two-sides',
    name: 'Two positions',
    projects: ['code'],
    params: {
      TOPIC: { type: 'text', label: 'Question' },
      ROUNDS: { type: 'int', label: 'Depth', min: 1, max: 3, default: 1 },
    },
    task: 'Decide: ${TOPIC}',
    seats: [
      { id: 'lead', job: 'write', character: 'domain_architect' },
      { id: 'critic-1', job: 'critique', character: 'critic_redteam' },
    ],
    close: { character: 'jev_mcp_decision' },
    links: [],
    ...over,
  };
  return { id: data.id, name: data.name, source: 'template', data, hash: 'h1', calls: 3, runnable: true, run_error: null, errors: [], warnings: [], ...entry };
};
const opsOnly = flow({ id: 'ops-only', name: 'Ops only', projects: ['ops'] });
const notRunnable = flow({ id: 'later', name: 'Later' }, { runnable: false, run_error: 'does not run yet' });
const teamFlow = flow({ id: 'cross-review', name: 'Cross review', projects: undefined, params: undefined, task: undefined }, { source: 'team' });
const list = (flows: FlowEntry[]): FlowList => ({
  flows,
  catalog: { jobs: [], critique_modes: [], input_modes: [], history_modes: [], limits: {} as FlowList['catalog']['limits'], categories: [], project_kinds: [], param_types: [] },
});
const personas = {
  personas: [
    { id: 'domain_architect', name: 'Architect' },
    { id: 'critic_redteam', name: 'Red team' },
    { id: 'devops_specialist', name: 'DevOps' },
    { id: 'jev_mcp_decision', name: 'Judge' },
    { id: 'only_critic', name: 'Only critic', format: 2 as const, seats: ['critic' as const] },
  ],
};

async function mount() {
  render(
    <GuardianRoot>
      <AppStores>
        <DebatesPage />
      </AppStores>
    </GuardianRoot>
  );
  await settle();
  if (!screen.queryByRole('button', { name: D.start.start })) fireEvent.click(screen.getByRole('button', { name: D.page.newDebate }));
}

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });
const start = () => fireEvent.click(screen.getByRole('button', { name: D.start.start }));
const alertText = () => within(screen.getByRole('region', { name: D.page.newDebate })).getByRole('alert').textContent;

beforeEach(() => {
  for (const fn of [mocked.fetchTeams, mocked.startDebate, mocked.fetchFlows, mocked.fetchPersonas]) fn.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-04T10:00:05Z'));
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchPersonas.mockResolvedValue(personas as never);
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.listActions.mockResolvedValue([]);
  mocked.fetchPublications.mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Start from a flow', () => {
  it('shows no picker when the hub has no runnable flow for the project', async () => {
    mocked.fetchFlows.mockResolvedValue(list([opsOnly, notRunnable]));
    await mount();
    expect(screen.queryByLabelText(F.start.label)).toBeNull();
    expect(screen.getByLabelText(D.start.task)).toBeTruthy();
  });

  it('tolerates a hub without the endpoint', async () => {
    mocked.fetchFlows.mockRejectedValue(new api.HubError('not found', 404));
    await mount();
    expect(screen.queryByLabelText(F.start.label)).toBeNull();
    expect(screen.getByLabelText(D.start.task)).toBeTruthy();
  });

  it('swaps the task for the parameters and sends flow, params and swapped seats', async () => {
    mocked.fetchFlows.mockResolvedValue(list([flow(), opsOnly, notRunnable]));
    mocked.startDebate.mockResolvedValue(runningJob);
    await mount();

    const picker = screen.getByLabelText(F.start.label) as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['', 'two-sides']);
    change(picker, 'two-sides');

    expect(screen.queryByLabelText(D.start.task)).toBeNull();
    expect(screen.queryByLabelText(D.start.continueThread)).toBeNull();
    expect((screen.getByLabelText('Depth') as HTMLInputElement).value).toBe('1');

    start();
    expect(alertText()).toBe(D.start.template.required('Question'));
    expect(mocked.startDebate).not.toHaveBeenCalled();

    change(screen.getByLabelText(new RegExp('^Question')), '  monolith or services?  ');
    change(screen.getByLabelText('Depth'), '5');
    start();
    expect(alertText()).toBe(D.start.template.range('Depth', 1, 3));
    change(screen.getByLabelText('Depth'), '2');

    // the critic seat offers characters that can critique, the default first
    fireEvent.click(screen.getByText(F.start.seats));
    const critic = screen.getByLabelText(`critic-1 · ${F.job.critique}`) as HTMLSelectElement;
    expect(Array.from(critic.options).map((o) => o.value)).toContain('only_critic');
    const lead = screen.getByLabelText(`lead · ${F.job.write}`) as HTMLSelectElement;
    expect(Array.from(lead.options).map((o) => o.value)).not.toContain('only_critic');
    change(critic, 'devops_specialist');

    start();
    await settle();
    expect(mocked.startDebate).toHaveBeenCalledWith({
      flow: 'two-sides',
      params: { TOPIC: 'monolith or services?', ROUNDS: '2' },
      seats: { 'critic-1': 'devops_specialist' },
      project: 'server-guardian',
    });
  });

  it('asks for the task when the flow has no text of its own', async () => {
    mocked.fetchFlows.mockResolvedValue(list([teamFlow]));
    mocked.startDebate.mockResolvedValue(runningJob);
    await mount();
    change(screen.getByLabelText(F.start.label), 'cross-review');
    change(screen.getByLabelText(D.start.task), 'Review the handler');
    start();
    await settle();
    expect(mocked.startDebate).toHaveBeenCalledWith({ flow: 'cross-review', task: 'Review the handler', project: 'server-guardian' });
  });

  it('returns to the free task', async () => {
    mocked.fetchFlows.mockResolvedValue(list([flow()]));
    await mount();
    const picker = screen.getByLabelText(F.start.label);
    change(picker, 'two-sides');
    change(picker, '');
    expect(screen.getByLabelText(D.start.task)).toBeTruthy();
    expect(screen.getByText(D.start.projectFlowNone)).toBeTruthy();
    expect(screen.queryByLabelText('Depth')).toBeNull();
  });
});

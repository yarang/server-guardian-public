import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FlowEntry, FlowList } from '../src/types';
import { ko } from '../src/i18n/ko';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { FlowsTab } from '../src/pages/flows/FlowsTab';

const mocked = vi.mocked(api);
const F = ko.flows;

beforeAll(() => {
  // xyflow measures its viewport; jsdom has neither ResizeObserver nor DOMMatrixReadOnly.
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: typeof RO }).ResizeObserver = RO;
  (globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly = class {
    m22 = 1;
    constructor() {}
  };
});

const teamFlow: FlowEntry = {
  id: 'default',
  name: 'Default lineup',
  source: 'team',
  hash: 'h',
  calls: 3,
  runnable: true,
  run_error: null,
  errors: [],
  warnings: [],
  data: {
    id: 'default',
    name: 'Default lineup',
    category: 'development',
    seats: [
      { id: 'lead', job: 'write', character: 'architect', providers: [] },
      { id: 'critic-1', job: 'critique', character: 'redteam', providers: [] },
    ],
    close: { character: 'judge', providers: [] },
    links: [
      { from: 'start', to: 'lead' },
      { from: 'lead', to: 'critic-1' },
      { from: 'critic-1', to: 'lead', look_back: true },
      { from: 'critic-1', to: 'close' },
      { from: 'close', to: 'lead', exit: 'rejected' },
      { from: 'close', to: 'end', exit: 'approved' },
    ],
    converted_from: { team: 'default' },
  },
};
const list: FlowList = {
  flows: [teamFlow, { ...teamFlow, id: 'later', name: 'Later', source: 'custom', runnable: false, run_error: 'does not run yet', data: { ...teamFlow.data, id: 'later', name: 'Later' } }],
  catalog: { jobs: [], critique_modes: [], input_modes: [], history_modes: [], limits: {} as FlowList['catalog']['limits'], categories: [], project_kinds: [], param_types: [] },
};

const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mocked.fetchFlows.mockResolvedValue(list);
  mocked.fetchPersonas.mockResolvedValue({ personas: [
    { id: 'architect', name: 'Architect' },
    { id: 'redteam', name: 'Red team' },
    { id: 'devops', name: 'DevOps' },
    { id: 'judge', name: 'Judge' },
  ] });
  mocked.fetchTeams.mockResolvedValue({ teams: [], catalog: { providers: ['claude', 'codex'], installed: [], tools: [{ name: 'jev_mcp:run_tests', risk: 'medium', description: '' }], max_critics: 4, max_rounds: 10 } });
  mocked.saveFlow.mockReset();
  mocked.saveFlow.mockResolvedValue({ id: 'default', warnings: [] });
});

describe('Flow kinds tab', () => {
  it('lists flows with their shape, source and run state', async () => {
    render(<GuardianRoot><FlowsTab /></GuardianRoot>);
    await settle();
    expect(screen.getByText('Default lineup')).toBeTruthy();
    expect(screen.getAllByText(F.source.team).length).toBeGreaterThan(0);
    expect(screen.getByText(F.summary.notRunnable)).toBeTruthy();
    expect(screen.getAllByText(new RegExp(`${F.job.write} → ${F.job.critique} → ${F.fixed.close}`)).length).toBeGreaterThan(0);
    // a team is copied, not edited; only a custom flow can be deleted
    expect(screen.getByRole('button', { name: F.copyAria('default') })).toBeTruthy();
    expect(screen.queryByRole('button', { name: F.deleteAria('default') })).toBeNull();
    expect(screen.getByRole('button', { name: F.deleteAria('later') })).toBeTruthy();
  });

  it('copies a team into a flow: add a seat, seat a character, save with the layout', async () => {
    render(<GuardianRoot><FlowsTab /></GuardianRoot>);
    await settle();
    fireEvent.click(screen.getByRole('button', { name: F.copyAria('default') }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(F.editor.subBuiltin)).toBeTruthy();
    expect(within(dialog).getByRole('group', { name: F.editor.canvasAria })).toBeTruthy();

    fireEvent.click(within(dialog).getByRole('button', { name: F.editor.addSeat(F.job.critique) }));
    expect(within(dialog).getByText(F.editor.seatPanel('critique-1'))).toBeTruthy();
    // the new seat has no character and is not linked yet: the problems are named before saving
    expect(within(dialog).getByText(new RegExp(F.issue.noCharacter))).toBeTruthy();
    expect(within(dialog).getByText(new RegExp(F.issue.neverRuns))).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText(F.editor.character), { target: { value: 'devops' } });
    fireEvent.change(within(dialog).getByLabelText(F.editor.bundle), { target: { value: 'new' } });

    fireEvent.click(within(dialog).getByRole('button', { name: F.editor.saveCopy }));
    await settle();
    const saved = mocked.saveFlow.mock.calls[0][0];
    expect(saved.id).toBe('default');
    expect(saved.seats.at(-1)).toEqual({ id: 'critique-1', job: 'critique', character: 'devops', providers: [], mode: 'object' });
    expect(saved.bundles).toEqual([{ id: 'bundle-1', seats: ['critique-1'] }]);
    expect(Object.keys(saved.layout ?? {})).toEqual(expect.arrayContaining(['start', 'lead', 'close', 'end']));
  });
});

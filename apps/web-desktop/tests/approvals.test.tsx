import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import { action, personaList, teamList } from './debateFixtures';
import { diagnosis, metrics, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { App } from '../src/App';
import { PROJECT_STORAGE_KEY } from '../src/lib/project';
import { ApprovalsPage } from '../src/pages/ApprovalsPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { APPROVALS_POLL_MS, useApprovalsStore } from '../src/state/approvalsStore';

const mocked = vi.mocked(api);

const twoProjects = {
  custom_dir: '/x',
  projects: [
    { id: 'server-guardian', source: 'builtin' as const, data: { id: 'server-guardian' }, errors: [] },
    { id: 'svc', source: 'custom' as const, data: { id: 'svc' }, errors: [] },
  ],
};

/** The ApprovalsPage the way App renders it: `#/approvals/:id` becomes the actionId prop. */
function Routed() {
  return <ApprovalsPage actionId={useRoute().params[0]} />;
}

const renderPage = () =>
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );

const proposed = (id: number, over = {}) => action({ id, status: 'PROPOSED', reason: `proposed ${id}`, ...over });
const held = (id: number) => action({ id, status: 'HELD', reason: `held ${id}` });
const done = (id: number, status = 'EXECUTED') => action({ id, status, reason: `done ${id}` });
const rowReasons = () => within(screen.getByRole('table', { name: ko.debate.actions.title })).getAllByRole('row').slice(1).map((r) => r.textContent ?? '');

beforeEach(() => {
  for (const fn of [mocked.listActions, mocked.getAction, mocked.rejectAction, mocked.fetchProjects]) fn.mockReset();
  mocked.listActions.mockResolvedValue([]);
  mocked.fetchProjects.mockResolvedValue({ projects: [], custom_dir: '/x' });
  mocked.fetchDebates.mockResolvedValue([]);
});

describe('Approvals list', () => {
  it('orders actions awaiting decision first, then held, then decided ones', async () => {
    mocked.listActions.mockResolvedValue([done(1), held(2), proposed(3), done(4, 'REJECTED'), proposed(5)]);
    renderPage();
    await screen.findByRole('table', { name: ko.debate.actions.title });
    fireEvent.click(within(await screen.findByRole('group', { name: ko.debate.actions.statusFilter })).getByRole('button', { name: ko.debate.actions.filterAll }));
    const ids = rowReasons().map((t) => /#(\d+)/.exec(t)?.[1]);
    expect(ids).toEqual(['3', '5', '2', '1', '4']);
  });

  it('filters by status and says how many of all are shown', async () => {
    mocked.listActions.mockResolvedValue([done(1), held(2), proposed(3), proposed(5)]);
    renderPage();
    await screen.findByRole('table', { name: ko.debate.actions.title });
    const filter = screen.getByRole('group', { name: ko.debate.actions.statusFilter });
    // Proposals are waiting, so the list opens on them.
    expect(await screen.findByText(ko.debate.actions.countOf(2, 4))).toBeTruthy();
    fireEvent.click(within(filter).getByRole('button', { name: ko.debate.actions.filterAll }));
    expect(screen.getByText(ko.debate.actions.count(4))).toBeTruthy();
    const calls = mocked.listActions.mock.calls.length;

    fireEvent.click(within(filter).getByRole('button', { name: ko.debate.actions.filterAwaiting }));
    expect(rowReasons()).toHaveLength(2);
    expect(screen.getByText(ko.debate.actions.countOf(2, 4))).toBeTruthy();

    fireEvent.click(within(filter).getByRole('button', { name: ko.debate.actions.filterHeld }));
    expect(rowReasons()).toHaveLength(1);
    expect(screen.getByText(ko.debate.actions.countOf(1, 4))).toBeTruthy();

    fireEvent.click(within(filter).getByRole('button', { name: ko.debate.actions.filterDecided }));
    expect(rowReasons()[0]).toContain('done 1');
    // Filtering is local: no refetch.
    expect(mocked.listActions).toHaveBeenCalledTimes(calls);
  });

  it('lists actions of the selected project and validates the thread filter', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mocked.fetchProjects.mockResolvedValue(twoProjects);
    mocked.listActions.mockResolvedValue([action({ id: 9, thread_id: 'thread_x', reason: 'verify', risk: 'low' })]);
    renderPage();
    const table = await screen.findByRole('table', { name: ko.debate.actions.title });
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'svc'));
    expect(within(table).getByText('verify')).toBeTruthy();
    expect(within(table).getByText(ko.debate.lib.risk.low)).toBeTruthy();
    expect(within(table).getByText(ko.debate.actionStatus.PROPOSED)).toBeTruthy();

    const panel = screen.getByRole('region', { name: ko.debate.actions.title });
    const reload = () => fireEvent.click(within(panel).getByRole('button', { name: ko.common.apply }));
    const calls = mocked.listActions.mock.calls.length;
    fireEvent.change(screen.getByLabelText(ko.debate.actions.filterThread), { target: { value: 'oops' } });
    reload();
    expect(screen.getByText(ko.debate.actions.badThreadFilter)).toBeTruthy();
    expect(mocked.listActions.mock.calls.length).toBe(calls);

    fireEvent.change(screen.getByLabelText(ko.debate.actions.filterThread), { target: { value: 'thread_x' } });
    reload();
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith('thread_x', 'svc'));
  });

  it('shows a list failure', async () => {
    mocked.listActions.mockRejectedValue(new Error('actions down'));
    renderPage();
    expect((await screen.findAllByText('actions down')).length).toBeGreaterThan(0);
  });
});

describe('Approvals default filter', () => {
  it('shows every action when none awaits a decision', async () => {
    mocked.listActions.mockResolvedValue([done(1), held(2)]);
    renderPage();
    expect(await screen.findByText(ko.debate.actions.count(2))).toBeTruthy();
    expect(rowReasons()).toHaveLength(2);
  });

  it('keeps the filter the operator chose when the list reloads', async () => {
    mocked.listActions.mockResolvedValue([done(1), proposed(2)]);
    renderPage();
    expect(await screen.findByText(ko.debate.actions.countOf(1, 2))).toBeTruthy();
    const filter = screen.getByRole('group', { name: ko.debate.actions.statusFilter });
    fireEvent.click(within(filter).getByRole('button', { name: ko.debate.actions.filterDecided }));
    mocked.listActions.mockResolvedValue([done(1), done(2)]);
    fireEvent.click(within(screen.getByRole('region', { name: ko.debate.actions.title })).getByRole('button', { name: ko.common.apply }));
    expect(await screen.findByText(ko.debate.actions.countOf(2, 2))).toBeTruthy();
  });
});

describe('Approvals URL', () => {
  it('opening an action from the list updates the URL and shows the action dialog', async () => {
    mocked.listActions.mockResolvedValue([proposed(9)]);
    mocked.getAction.mockResolvedValue(proposed(9));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.result.openAction(9) }));
    expect(window.location.hash).toBe('#/approvals/9');
    expect(await screen.findByRole('dialog', { name: ko.debate.dialog.title(9) })).toBeTruthy();
    expect(mocked.getAction).toHaveBeenCalledWith(9);
  });

  it('opens #/approvals/:id directly, over the list, and closing returns to #/approvals', async () => {
    window.location.hash = '#/approvals/9';
    mocked.listActions.mockResolvedValue([proposed(9)]);
    mocked.getAction.mockResolvedValue(proposed(9));
    renderPage();
    const dialog = await screen.findByRole('dialog', { name: ko.debate.dialog.title(9) });
    expect(screen.getByRole('table', { name: ko.debate.actions.title })).toBeTruthy();

    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.close }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(window.location.hash).toBe('#/approvals');
  });

  it('follows the URL when another action is opened', async () => {
    window.location.hash = '#/approvals/9';
    mocked.listActions.mockResolvedValue([proposed(9), proposed(10)]);
    mocked.getAction.mockImplementation(async (id: number) => proposed(id));
    renderPage();
    await screen.findByRole('dialog', { name: ko.debate.dialog.title(9) });
    window.location.hash = '#/approvals/10';
    expect(await screen.findByRole('dialog', { name: ko.debate.dialog.title(10) })).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: ko.debate.dialog.title(9) })).toBeNull();
  });

  it('says so for an id that is not a number and opens no dialog', async () => {
    window.location.hash = '#/approvals/abc';
    renderPage();
    expect(await screen.findByText(ko.ops.approvals.invalidId('abc'))).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocked.getAction).not.toHaveBeenCalled();
  });

  it('refreshes the shared actions after a reject so counts update', async () => {
    window.location.hash = '#/approvals/9';
    mocked.listActions.mockResolvedValue([proposed(9)]);
    mocked.getAction.mockResolvedValue(proposed(9));
    mocked.rejectAction.mockResolvedValue(done(9, 'REJECTED'));
    renderPage();
    const dialog = await screen.findByRole('dialog', { name: ko.debate.dialog.title(9) });
    await waitFor(() => expect(mocked.listActions).toHaveBeenCalled());
    const before = mocked.listActions.mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.dialog.reject }));
    fireEvent.click(within(screen.getByRole('dialog', { name: ko.debate.dialog.rejectQ(9) })).getByRole('button', { name: ko.debate.dialog.rejectAction }));
    await waitFor(() => expect(mocked.rejectAction).toHaveBeenCalledWith(9));
    await waitFor(() => expect(mocked.listActions.mock.calls.length).toBeGreaterThan(before));
  });
});

describe('Approvals badge', () => {
  const renderApp = () =>
    render(
      <GuardianRoot>
        <App />
      </GuardianRoot>
    );
  const approvalsLink = () =>
    within(screen.getByRole('navigation', { name: ko.shell.nav.primary })).getByRole('link', { name: new RegExp('^' + ko.shell.nav.routes.approvals.label) });

  beforeEach(() => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchPersonas.mockResolvedValue(personaList);
    mocked.fetchTeams.mockResolvedValue(teamList);
  });

  it('shows the number of PROPOSED actions of the selected project (held and decided excluded)', async () => {
    mocked.listActions.mockResolvedValue([proposed(1), proposed(2), held(3), done(4)]);
    renderApp();
    await waitFor(() => expect(approvalsLink().textContent).toBe(`${ko.shell.nav.routes.approvals.label}2`));
    expect(mocked.listActions).toHaveBeenCalledWith(undefined, 'server-guardian');
  });

  it('is hidden when nothing is waiting', async () => {
    mocked.listActions.mockResolvedValue([held(3), done(4)]);
    renderApp();
    await waitFor(() => expect(mocked.listActions).toHaveBeenCalled());
    await act(async () => undefined);
    expect(approvalsLink().textContent).toBe(ko.shell.nav.routes.approvals.label);
  });

  it('is hidden, not a guess, when the count is unknown', async () => {
    mocked.listActions.mockRejectedValue(new Error('actions down'));
    renderApp();
    await waitFor(() => expect(mocked.listActions).toHaveBeenCalled());
    await act(async () => undefined);
    expect(approvalsLink().textContent).toBe(ko.shell.nav.routes.approvals.label);
  });

  it('counts the remembered project, not the default one', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mocked.fetchProjects.mockResolvedValue(twoProjects);
    mocked.listActions.mockImplementation(async (_thread, project) => (project === 'svc' ? [proposed(1), proposed(2), proposed(3)] : [proposed(1)]));
    renderApp();
    await waitFor(() => expect(approvalsLink().textContent).toBe(`${ko.shell.nav.routes.approvals.label}3`));
  });
});

describe('Approvals store polling', () => {
  function Probe() {
    const { waiting, refresh } = useApprovalsStore();
    return (
      <>
        <span data-testid="waiting">{waiting === null ? 'unknown' : String(waiting)}</span>
        <button onClick={refresh}>refresh</button>
      </>
    );
  }
  const mountProbe = () =>
    render(
      <GuardianRoot>
        <AppStores>
          <Probe />
        </AppStores>
      </GuardianRoot>
    );

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const settle = (ms = 0) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  it('polls every 30 s, refetches on refresh() and stops when the provider unmounts', async () => {
    mocked.listActions.mockResolvedValueOnce([proposed(1)]);
    mocked.listActions.mockResolvedValue([proposed(1), proposed(2)]);
    const view = mountProbe();
    await settle();
    expect(screen.getByTestId('waiting').textContent).toBe('1');
    expect(mocked.listActions).toHaveBeenCalledTimes(1);

    await settle(APPROVALS_POLL_MS - 1);
    expect(mocked.listActions).toHaveBeenCalledTimes(1);
    await settle(1);
    expect(mocked.listActions).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('waiting').textContent).toBe('2');

    fireEvent.click(screen.getByText('refresh'));
    await settle();
    expect(mocked.listActions).toHaveBeenCalledTimes(3);

    view.unmount();
    await settle(APPROVALS_POLL_MS * 3);
    expect(mocked.listActions).toHaveBeenCalledTimes(3);
  });
});

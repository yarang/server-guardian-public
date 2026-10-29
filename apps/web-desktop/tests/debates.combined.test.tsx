import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { action, doneJob, runningJob, teamList } from './debateFixtures';
import { follow } from './nav';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { setDebatePrefill } from '../src/state/debatePrefill';
import { ko } from '../src/i18n/ko';

const D = ko.debate;

const mocked = vi.mocked(api);
const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** The page the way App renders it: `#/debates/:jobId` becomes the jobId prop. */
function Routed() {
  return <DebatesPage jobId={useRoute().params[0]} />;
}

const mount = async () => {
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle();
};

const runningRow = { ...runningJob, id: 'jobR', task: 'Still going', events: true };
const finishedRow = { ...doneJob, id: 'jobF', task: 'All done' };

beforeEach(() => {
  for (const fn of [mocked.fetchDebates, mocked.fetchDebate, mocked.listActions, mocked.getPublishPlan, mocked.pollDebateEvents]) fn.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-04T10:00:05Z'));
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchPublications.mockResolvedValue([]);
  mocked.pollDebateEvents.mockResolvedValue({ events: [], next: 0, done: true });
  mocked.fetchDebates.mockResolvedValue([finishedRow, runningRow]);
  mocked.fetchDebate.mockImplementation(async (id) => (id === 'jobR' ? { ...runningRow, log: '', result: null } : { ...finishedRow, id }));
  mocked.listActions.mockResolvedValue([action({ id: 41, tool: 'jev_mcp:apply_patch', status: 'EXECUTED' })]);
});
afterEach(() => vi.useRealTimers());

describe('Debates: one list, one detail', () => {
  it('selects the newest running debate by default and shows its live monitor', async () => {
    await mount();
    const rows = within(screen.getByRole('navigation', { name: ko.agents.list.aria })).getAllByRole('link');
    expect(rows[0].getAttribute('aria-label')).toBe(ko.agents.list.openDebate('jobR'));
    expect(rows[0].getAttribute('aria-current')).toBe('true');
    expect(screen.getByTestId('run-elapsed')).toBeTruthy();
    expect(screen.queryByRole('region', { name: D.result.title })).toBeNull();
  });

  it('shows the result with Publish for a finished debate, and the monitor again for a running one', async () => {
    await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('jobF') }));
    await settle();
    expect(window.location.hash).toBe('#/debates/jobF');
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: D.result.publish })).toBeTruthy();
    expect(screen.queryByTestId('run-elapsed')).toBeNull();

    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('jobR') }));
    await settle();
    expect(screen.getByTestId('run-elapsed')).toBeTruthy();
    expect(screen.queryByRole('region', { name: D.result.title })).toBeNull();
  });

  it('opens a deep link #/debates/<id> on the result and keeps the recorded run behind "Agent activity"', async () => {
    window.location.hash = '#/debates/jobF';
    await mount();
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
    expect(screen.queryByTestId('run-elapsed')).toBeNull();
    fireEvent.click(screen.getByText(D.page.activity));
    await settle();
    expect(screen.getByTestId('run-elapsed')).toBeTruthy();
  });

  it('keeps the New debate form collapsed until asked, and a hand-off from another page expands it', async () => {
    await mount();
    expect(screen.queryByRole('button', { name: D.start.start })).toBeNull();
    const toggle = screen.getByRole('button', { name: D.page.newDebate });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('button', { name: D.start.start })).toBeTruthy();
  });

  it('expands the form by itself when another page hands over a task', async () => {
    setDebatePrefill('Review the fleet', 'fleet');
    await mount();
    expect(screen.getByRole('button', { name: D.page.newDebate }).getAttribute('aria-expanded')).toBe('true');
    expect((screen.getByLabelText(D.start.task) as HTMLTextAreaElement).value).toBe('Review the fleet');
  });

  it('fills the thread id and opens the form when "Continue this thread" is used on a result', async () => {
    window.location.hash = '#/debates/jobF';
    await mount();
    fireEvent.click(screen.getByRole('button', { name: D.result.continueThread }));
    await settle();
    expect(screen.getByRole('button', { name: D.page.newDebate }).getAttribute('aria-expanded')).toBe('true');
    expect((screen.getByLabelText(D.start.continueThread) as HTMLInputElement).value).toBe('thread_abc123');
  });
});

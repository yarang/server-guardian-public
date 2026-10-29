import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { doneJob, runningJob } from './debateFixtures';
import type { DebateEvent, DebateJob, DebateStreamMessage } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import { ko } from '../src/i18n/ko';
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);
const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const T0 = '2026-10-04T10:00:00Z';
const t = (s: number) => new Date(Date.parse(T0) + s * 1000).toISOString();
const expert = { round: 1, role: 'expert', agent: 'expert', agent_name: 'Expert' };

const history: DebateEvent[] = [
  {
    seq: 1, ts: t(0), type: 'debate_started',
    data: {
      task: 'Harden sshd', project: 'server-guardian', team: 'cross-review', max_rounds: 3,
      members: [
        { role: 'expert', agent: 'expert', agent_name: 'Expert', providers: ['claude', 'agy'] },
        { role: 'judge', agent: 'judge', agent_name: 'Judge', providers: ['claude'] },
      ],
      context: { sha: 'abc1234def', files: 3 },
    },
  },
  { seq: 2, ts: t(1), type: 'round_started', round: 1 },
  { seq: 3, ts: t(2), type: 'agent_started', ...expert, data: { prompt_bytes: 1024 } },
  { seq: 4, ts: t(3), type: 'provider_failed', ...expert, data: { provider: 'claude', error: 'rate limited' } },
  { seq: 5, ts: t(4), type: 'retrieval_requested', ...expert, data: { step: 1, requests: [{ op: 'read', target: 'apps/agent/main.go:100-180' }, { op: 'read', target: '.env' }] } },
  {
    seq: 6, ts: t(5), type: 'retrieval_served', ...expert,
    data: { step: 1, turn_bytes: 2048, results: [{ op: 'read', target: 'apps/agent/main.go:100-180', ok: true, bytes: 2048, truncated: true }, { op: 'read', target: '.env', ok: false, bytes: 0, error: 'excluded' }] },
  },
] as DebateEvent[];

const finishedEvents: DebateEvent[] = [
  ...history,
  { seq: 7, ts: t(9), type: 'agent_finished', ...expert, data: { provider: 'agy', duration_ms: 7000, steps: 1, output_bytes: 900, excerpt: 'Use PermitRootLogin no', fallback: false } },
  { seq: 8, ts: t(20), type: 'verdict', data: { approved: true, unverified: false } },
  { seq: 9, ts: t(21), type: 'jev_gate', data: { gate: 'PASSED', reasons: [] } },
  { seq: 10, ts: t(22), type: 'debate_finished', data: { status: 'completed', consensus: true, final_round: 1 } },
] as DebateEvent[];

const listRunning: DebateJob = { ...runningJob, project: 'server-guardian', events: true };
const listDone: DebateJob = { ...doneJob, id: 'job2', events: true };
// The fetched detail of the finished job: same id as the list row, so the page treats it as the watched job.
const doneJob2 = { ...doneJob, id: 'job2' };

/** A finished debate shows its result first; the recorded run (lanes, outcome, timeline) is behind this disclosure. */
async function openActivity() {
  fireEvent.click(screen.getByText(ko.debate.page.activity));
  await settle(300);
}

function mount(runId?: string) {
  return render(
    <GuardianRoot>
      <AppStores>
        <DebatesPage jobId={runId} />
      </AppStores>
    </GuardianRoot>
  );
}

beforeEach(() => {
  for (const fn of [mocked.fetchDebates, mocked.fetchDebate, mocked.cancelDebate, mocked.pollDebateEvents, mocked.streamDebateEvents]) fn.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(new Date(Date.parse(T0) + 30_000));
});
afterEach(() => vi.useRealTimers());

describe('Debates page: live monitor and finished runs', () => {
  it('lists running runs first and follows the selected running run live', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone, listRunning]);
    mocked.fetchDebate.mockResolvedValue({ ...listRunning, log: '', result: null });
    mocked.pollDebateEvents.mockImplementation(async (id, after) =>
      id === 'job1' ? { events: history.filter((e) => e.seq > after), next: 6, done: false } : { events: [], next: 0, done: true }
    );
    let push: (m: DebateStreamMessage) => void = () => undefined;
    mocked.streamDebateEvents.mockImplementation(
      (_id, _after, onEvent) => new Promise(() => { push = onEvent; })
    );
    mount();
    await settle(300);

    const nav = screen.getByRole('navigation', { name: ko.agents.list.aria });
    const rows = within(nav).getAllByRole('link');
    expect(rows[0].getAttribute('aria-label')).toBe(ko.agents.list.openDebate('job1'));
    expect(rows[0].getAttribute('aria-current')).toBe('true');

    // Lane card: retrieving state is back to running after the result, with steps and bytes.
    const lane = screen.getByRole('region', { name: ko.agents.lane.aria });
    expect(within(lane).getAllByText(ko.agents.laneState.running).length).toBeGreaterThan(0);
    expect(within(lane).getByText(`${ko.agents.lane.retrievalSteps(1)} · ${ko.agents.lane.thisTurn(1, '2.0 KB')}`)).toBeTruthy();
    expect(within(lane).getByText(ko.agents.lane.providerFailed('claude', 'rate limited'))).toBeTruthy();
    // Live seconds: lane started at +2s, clock at +30s.
    expect(screen.getByTestId('lane-elapsed-expert').textContent).toContain(ko.agents.lane.seconds(28));

    // Retrieval chips with the served result.
    expect(screen.getByText('read apps/agent/main.go:100-180')).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.truncatedChip)).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.excluded)).toBeTruthy();
    expect(screen.getByLabelText(ko.agents.timeline.roundAria(1))).toBeTruthy();

    // A streamed event updates the lane; the clock advances the elapsed seconds.
    act(() => push({ seq: 7, ts: t(31), type: 'agent_finished', ...expert, data: { provider: 'agy', duration_ms: 29000 } } as DebateEvent));
    await settle(300);
    expect(within(screen.getByRole('region', { name: ko.agents.lane.aria })).getAllByText(ko.agents.laneState.done).length).toBeGreaterThan(0);
    expect(mocked.streamDebateEvents.mock.calls[0][1]).toBe(6);
  });

  it('shows retrieval cost live in the run header and lane cards, preferring the finished totals', async () => {
    mocked.fetchDebates.mockResolvedValue([{ ...listRunning, retrieval: { results: 9, bytes: 3 * 1024 * 1024 } }, listDone]);
    mocked.fetchDebate.mockResolvedValue({ ...doneJob, id: 'job1', status: 'running' });
    mocked.pollDebateEvents.mockResolvedValue({ events: history, next: 6, done: false });
    mount('job1');
    await settle(300);
    // Live: only the one served result counts (the excluded read does not).
    expect(screen.getByTestId('run-retrieval').textContent).toBe(ko.common.retrieval(1, '2.0 KB'));
    const lane = screen.getByRole('region', { name: ko.agents.lane.aria });
    expect(within(lane).getByText(ko.agents.lane.runTotal(1, '2.0 KB'))).toBeTruthy();
    // The run list shows the list entry's retrieval.
    expect(within(screen.getByRole('navigation', { name: ko.agents.list.aria })).getByText(ko.common.retrieval(9, '3.0 MB'))).toBeTruthy();
  });

  it('prefers the debate_finished retrieval totals over the live sum', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    const events = finishedEvents.map((e) =>
      e.type === 'debate_finished'
        ? ({ ...e, data: { ...e.data, retrieval: { results: 176, bytes: 2 * 1024 * 1024, by_agent: { expert: { results: 176, bytes: 2 * 1024 * 1024, steps: 12 } } } } } as DebateEvent)
        : e
    );
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(screen.getByTestId('run-retrieval').textContent).toBe(ko.common.retrieval(176, '2.0 MB'));
    expect(screen.getByTestId('lane-total-expert').textContent).toBe(ko.agents.lane.runTotal(176, '2.0 MB'));
  });

  it('shows an MCP tag on the lane card and on timeline retrieval items served through MCP', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone, listRunning]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    const viaMcp = finishedEvents.map((e) =>
      e.type === 'retrieval_requested' || e.type === 'retrieval_served' ? ({ ...e, data: { ...e.data, via: 'mcp' } } as DebateEvent) : e
    );
    mocked.pollDebateEvents.mockResolvedValue({ events: viaMcp, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(within(screen.getByRole('region', { name: ko.agents.lane.aria })).getByText('MCP')).toBeTruthy();
    expect(screen.getAllByText('MCP')).toHaveLength(2);
  });

  it('labels the node checks of an ops debate on the timeline', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone, listRunning]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    const viaOps = finishedEvents.map((e) =>
      e.type === 'retrieval_requested' || e.type === 'retrieval_served' ? ({ ...e, data: { ...e.data, via: 'ops' } } as DebateEvent) : e
    );
    mocked.pollDebateEvents.mockResolvedValue({ events: viaOps, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(screen.getAllByText(ko.agents.timeline.checksNodes(1)).length).toBeGreaterThan(0);
    expect(screen.queryByText(ko.agents.timeline.readsCode(1))).toBeNull();
    expect(screen.queryByText('MCP')).toBeNull();
  });

  it('offers the result document of a finished debate and fetches it only when opened', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone, listRunning]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events: finishedEvents, next: 10, done: true });
    mocked.fetchThreadReport.mockRejectedValue(new api.HubError('report not found', 404));
    mount('job2');
    await settle(300);
    expect(mocked.fetchThreadReport).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    await settle(0);
    expect(mocked.fetchThreadReport).toHaveBeenCalledWith(doneJob2.thread_id);
    expect(screen.getByText(ko.debate.report.notFound)).toBeTruthy();
  });

  it('shows a finished run from one poll with outcome and output excerpt', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone, listRunning]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events: finishedEvents, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(mocked.streamDebateEvents).not.toHaveBeenCalled();
    expect(screen.getAllByText(ko.agents.detail.outcome).length).toBeGreaterThan(0);
    expect(screen.getAllByText(ko.debate.decisionLabel.APPROVED).length).toBeGreaterThan(0);
    expect(screen.getAllByText(ko.agents.gate.PASSED).length).toBeGreaterThan(0);
    expect(screen.getByText('Use PermitRootLogin no')).toBeTruthy();
    // No ping-pong links: the result is on this page.
    expect(screen.queryByRole('link', { name: 'Open debate result' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Monitor debate job2' })).toBeNull();
    expect(screen.queryByRole('button', { name: ko.shell.cancelDebate.cancel })).toBeNull();
  });

  it('loads the saved turn prompt only when it is opened', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    const events = finishedEvents.map((e) =>
      e.seq === 3 ? ({ ...e, data: { prompt_bytes: 1024, prompt_saved: true, prompt_truncated: true } } as DebateEvent) : e
    );
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 10, done: true });
    mocked.fetchDebatePrompt.mockReset();
    mocked.fetchDebatePrompt.mockResolvedValue({ seq: 3, bytes: 20, text: '=== Task ===\nHarden sshd' });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(mocked.fetchDebatePrompt).not.toHaveBeenCalled();
    const summary = screen.getByText(ko.agents.timeline.showPrompt);
    const details = summary.closest('details')!;
    details.open = true;
    fireEvent(details, new Event('toggle'));
    await settle(0);
    expect(mocked.fetchDebatePrompt).toHaveBeenCalledWith('job2', 3);
    expect(within(details).getByText(/Harden sshd/)).toBeTruthy();
    expect(within(details).getByText(ko.agents.timeline.promptTruncated)).toBeTruthy();
  });

  it('shows no prompt link for events recorded before prompts were saved', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events: finishedEvents, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(screen.queryByText(ko.agents.timeline.showPrompt)).toBeNull();
  });

  it.each(['REVISION_REQUIRED', 'IN_PROGRESS', 'NOT APPROVED'])('tones a completed run that was %s as not approved, not as success', async (word) => {
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue({ ...doneJob2, result: null });
    const events = finishedEvents.map((e) => {
      if (e.type === 'debate_finished') return { ...e, data: { ...e.data, status: word } } as DebateEvent;
      if (e.type === 'verdict') return { ...e, data: { approved: false, unverified: false } } as DebateEvent;
      if (e.type === 'jev_gate') return null;
      return e;
    }).filter((e): e is DebateEvent => e !== null);
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    const header = screen.getAllByText(ko.debate.decisionLabel['NOT APPROVED']).find((el) => el.className.includes('sg-chip--warn'));
    expect(header).toBeTruthy();
    // The job status chip in the run header follows the decision, not the exit of the job.
    const completed = screen.getAllByText(ko.agents.jobStatus.completed).map((el) => el.className);
    expect(completed.some((c) => c.includes('sg-chip--warn'))).toBe(true);
    expect(screen.queryByText(ko.debate.decisionLabel.APPROVED)).toBeNull();
  });

  it('tones the timeline row of an approved finish as success', async () => {
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    const events = finishedEvents.map((e) =>
      e.type === 'debate_finished' ? ({ ...e, data: { ...e.data, status: 'APPROVED' } } as DebateEvent) : e,
    );
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 10, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    const row = screen.getByText(ko.agents.timeline.debateFinished).parentElement as HTMLElement;
    const chip = Array.from(row.querySelectorAll('.sg-chip')).find((el) => el.textContent === ko.debate.decisionLabel.APPROVED);
    expect(chip?.className).toContain('sg-chip--ok');
  });

  it('lists the selected project, hides closed threads and shows them on request', async () => {
    const closed: DebateJob = { ...listDone, id: 'job9', task: 'Old closed task', thread_closed: true };
    mocked.fetchDebates.mockResolvedValue([listDone, closed]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events: finishedEvents, next: 10, done: true });
    mount();
    await settle(300);
    expect(mocked.fetchDebates).toHaveBeenCalledWith('server-guardian');
    const nav = () => screen.getByRole('navigation', { name: ko.agents.list.aria });
    expect(within(nav()).queryByRole('link', { name: ko.agents.list.openDebate('job9') })).toBeNull();
    expect(within(nav()).getByRole('link', { name: ko.agents.list.openDebate('job2') })).toBeTruthy();
    expect(screen.getByText(ko.debate.page.listTitleCount(1))).toBeTruthy();
    expect(screen.getByText(ko.debate.page.closedHidden(1))).toBeTruthy();

    fireEvent.click(screen.getByRole('checkbox', { name: ko.debate.page.showClosed }));
    expect(screen.getByText(ko.debate.page.listTitleCount(2))).toBeTruthy();
    expect(within(nav()).getByRole('link', { name: ko.agents.list.openDebate('job9') })).toBeTruthy();
    expect(screen.queryByText(ko.debate.page.closedHidden(1))).toBeNull();
  });

  it('loads the other project when the project is switched', async () => {
    mocked.fetchDebates.mockResolvedValue([]);
    mocked.fetchProjects.mockResolvedValue({
      custom_dir: '/x',
      projects: [
        { id: 'server-guardian', source: 'builtin', data: { id: 'server-guardian' }, errors: [] },
        { id: 'svc', source: 'custom', data: { id: 'svc' }, errors: [] },
      ],
    });
    mount();
    await settle(300);
    fireEvent.change(screen.getByLabelText(ko.shell.projectSwitcher.label), { target: { value: 'svc' } });
    await settle(300);
    expect(mocked.fetchDebates).toHaveBeenLastCalledWith('svc');
  });

  it('cancels a running run', async () => {
    mocked.fetchDebates.mockResolvedValue([listRunning]);
    mocked.fetchDebate.mockResolvedValue({ ...listRunning, log: '', result: null });
    mocked.pollDebateEvents.mockResolvedValue({ events: [], next: 0, done: false });
    mocked.streamDebateEvents.mockImplementation(() => new Promise(() => undefined));
    mocked.cancelDebate.mockResolvedValue(undefined);
    mount('job1');
    await settle(300);
    fireEvent.click(screen.getByRole('button', { name: ko.shell.cancelDebate.cancel }));
    expect(mocked.cancelDebate).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog', { name: ko.shell.cancelDebate.titleDebate })).getByRole('button', { name: ko.shell.cancelDebate.titleDebate }));
    await settle();
    expect(mocked.cancelDebate).toHaveBeenCalledWith('job1');
  });

  it('shows the empty state and the saved log for a run without events', async () => {
    mocked.fetchDebates.mockResolvedValue([{ ...listDone, events: false }]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mount('job2');
    await settle(300);
    await openActivity();
    expect(screen.getByText(ko.agents.detail.noRecordTitle)).toBeTruthy();
    expect(screen.getAllByText(/round 1 done/).length).toBeGreaterThan(0);
    expect(mocked.pollDebateEvents).not.toHaveBeenCalled();
  });

  it('shows the empty state when the events file turns out to be empty', async () => {
    mocked.fetchDebates.mockResolvedValue([{ ...listDone, events: undefined }]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events: [], next: 0, done: true });
    mount('job2');
    await settle(300);
    await openActivity();
    expect(screen.getByText(ko.agents.detail.noRecordTitle)).toBeTruthy();
  });

  it('renders deferred, legacy overflow, budget, trimmed and citation signals', async () => {
    const events = [
      {
        seq: 1, ts: t(0), type: 'debate_started',
        data: { task: 't', project: 'p', team: 'x', max_rounds: 1, members: [{ role: 'expert', agent: 'expert', agent_name: 'Expert', providers: ['claude'] }], context: { sha: 'abc', files: 0 } },
      },
      { seq: 2, ts: t(1), type: 'round_started', round: 1 },
      { seq: 3, ts: t(2), type: 'agent_started', ...expert, data: {} },
      { seq: 4, ts: t(3), type: 'retrieval_requested', ...expert, data: { step: 1, requests: [{ op: 'read', target: 'a.go' }, { op: 'read', target: 'b.go' }, { op: 'read', target: 'c.go' }, { op: '?', target: 'repo block' }] } },
      {
        seq: 5, ts: t(4), type: 'retrieval_served', ...expert,
        data: {
          step: 1, turn_bytes: 5,
          results: [
            { op: 'read', target: 'a.go', ok: true, bytes: 5 },
            { op: 'read', target: 'b.go', ok: false, bytes: 0, error: 'deferred', deferred: true },
            { op: 'read', target: 'c.go', ok: false, bytes: 0, error: 'budget' },
            { op: '?', target: 'repo block', ok: false, bytes: 0, error: 'too many requests in one reply' },
          ],
        },
      },
      { seq: 6, ts: t(5), type: 'reply_trimmed', ...expert, data: { step: 1, discarded_chars: 6400, blocks: 3 } },
      { seq: 7, ts: t(6), type: 'citation_check', ...expert, data: { cited: 2, unverified: ['z.go:9'], asked: false } },
      { seq: 8, ts: t(7), type: 'citation_check', ...expert, data: { cited: 2, unverified: [], asked: true } },
      { seq: 9, ts: t(8), type: 'agent_finished', ...expert, data: { provider: 'claude', duration_ms: 1000, steps: 0 } },
      { seq: 10, ts: t(9), type: 'pseudo_tool_call', ...expert, data: { step: 1, tools: ['glob', 'ls'], discarded_chars: 250 } },
      { seq: 11, ts: t(10), type: 'pseudo_tool_call', ...expert, data: { step: 2, tools: [], discarded_chars: 10 } },
      { seq: 12, ts: t(11), type: 'format_retry', role: 'judge', agent: 'judge', agent_name: 'Judge', round: 1, data: { missing: ['verdict'] } },
      { seq: 13, ts: t(12), type: 'action_repair', role: 'judge', agent: 'judge', agent_name: 'Judge', round: 1, data: { invalid: [{ index: 0, tool: 'apply_patch', error: 'old text not unique' }], fixed: 0 } },
    ] as DebateEvent[];
    mocked.fetchDebates.mockResolvedValue([listDone]);
    mocked.fetchDebate.mockResolvedValue(doneJob2);
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 13, done: true });
    mount('job2');
    await settle(300);
    await openActivity();

    expect(screen.getByText(ko.agents.timeline.deferred)).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.budget)).toBeTruthy();
    expect(screen.getByText('too many requests in one reply')).toBeTruthy();
    expect(screen.queryByText('? repo block')).toBeNull();
    expect(screen.getByText(ko.agents.timeline.replyTrimmed('Expert', 6400, 3))).toBeTruthy();
    expect(screen.getByText(/z\.go:9/)).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.citationsVerified(2))).toBeTruthy();
    const lane = screen.getByRole('region', { name: ko.agents.lane.aria });
    expect(within(lane).getByText(ko.agents.lane.deferred(1))).toBeTruthy();
    expect(within(lane).getByText(ko.agents.lane.trimmed(1))).toBeTruthy();
    expect(within(lane).getByText(ko.agents.lane.fakeCalls(2))).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.pseudoCall('Expert', 'glob, ls', 250))).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.actionRepair('Judge', 1, 0))).toBeTruthy();
    expect(screen.getByText(/#0 apply_patch: old text not unique/)).toBeTruthy();
    expect(screen.getByText(ko.agents.timeline.formatRetry('Judge', 'verdict'))).toBeTruthy();
    expect(within(lane).getByText(ko.agents.lane.noCodeRead)).toBeTruthy();
    // The later clean check cleared the unverified badge.
    expect(within(lane).queryByText(ko.agents.lane.unverifiedCitations(1, false))).toBeNull();
  });
});

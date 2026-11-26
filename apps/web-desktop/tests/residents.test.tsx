import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ActionItem, ProjectEntry, ResidentAgent, ResidentMonitor, ResidentTask } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { follow } from './nav';

const mocked = vi.mocked(api);
const R = ko.residents;
const D = ko.debate;

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function Routed() {
  const { params, query } = useRoute();
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} extra={params.slice(2)} query={query} />;
}

async function mount(hash: string) {
  window.location.hash = hash;
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>,
  );
  await settle(300);
}

const project = (id: string, kind?: 'design' | 'ops'): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id, kind }, errors: [], status: { present: true } }) as ProjectEntry;

const agent = (over: Partial<ResidentAgent> = {}): ResidentAgent => ({
  address: 'claude@node-1',
  name: 'claude',
  node: 'node-1',
  provider: 'claude',
  driver: 'headless',
  tools: 'closed',
  state: 'ready',
  epoch: 3,
  not_ready_since: null,
  bind_failures: 0,
  bind_started: null,
  modified: '2026-11-21T09:00:00Z',
  incarnation: { epoch: 3, started: '2026-11-20T08:00:00Z', pane: null, pid: 4242 },
  turn: null,
  mail_open: { queued: 1, delivered: 0, retrieved: 0 },
  tasks_held: 1,
  declared: true,
  in_store: true,
  ...over,
});

const snapshot = (over: Partial<ResidentMonitor> = {}): ResidentMonitor => ({
  generated: '2026-11-21T12:00:00Z',
  store: { exists: true, schema: 5, path_kind: 'data_dir' },
  agents: [
    agent(),
    agent({
      address: 'coder@node-1',
      name: 'coder',
      tools: 'open',
      state: 'down',
      epoch: 7,
      not_ready_since: '2026-11-21T11:00:00Z',
      bind_failures: 2,
      turn: { id: 't-1', kind: 'operator', started: '2026-11-21T11:59:30Z', hop_in: 0 },
      declared: false,
      mail_open: { queued: 0, delivered: 0, retrieved: 0 },
      tasks_held: 0,
    }),
  ],
  turns: {
    'claude@node-1': [
      { id: 't-9', epoch: 3, kind: 'wake', started: '2026-11-21T11:58:00Z', ended: '2026-11-21T11:59:00Z', end_reason: 'normal', hop_in: 0, inputs: 2 },
      { id: 't-8', epoch: 3, kind: 'operator', started: '2026-11-21T11:40:00Z', ended: null, end_reason: null, hop_in: 0, inputs: 1 },
    ],
  },
  mail: [
    {
      id: 'm-20261121-0001',
      thread: null,
      sender: 'operator',
      recipient: 'claude@node-1',
      kind: 'question',
      subject: '상태 점검',
      task: null,
      reply_to: null,
      hop: 0,
      state: 'retrieved',
      created: '2026-11-21T11:57:00Z',
      expires: null,
      redelivered: 0,
      bytes: 120,
    },
    {
      id: 'm-20261121-0002',
      thread: null,
      sender: 'claude@node-1',
      recipient: 'operator',
      kind: 'answer',
      subject: 'RE: 상태 점검',
      task: null,
      reply_to: 'm-20261121-0001',
      hop: 0,
      state: 'delivered',
      created: '2026-11-21T11:59:00Z',
      expires: null,
      redelivered: 0,
      bytes: 300,
    },
  ],
  tasks: [
    {
      id: 'k-1',
      title: '설명 보강',
      creator: 'operator',
      assignee: 'claude@node-1',
      owner: null,
      status: 'pending',
      project: 'server-guardian',
      sha: '0123456789abcdef',
      hop: 0,
      generation: 0,
      blocked_by: [],
      patch_bytes: null,
      patch_action: null,
      calls: null,
      reason: null,
      created: '2026-11-21T10:00:00Z',
      modified: '2026-11-21T10:00:00Z',
      expires: null,
    },
    {
      id: 'k-2',
      title: '테스트 추가',
      creator: 'operator',
      assignee: 'coder@node-1',
      owner: 'coder@node-1',
      status: 'completed',
      project: 'server-guardian',
      sha: '0123456789abcdef',
      hop: 1,
      generation: 1,
      blocked_by: [],
      patch_bytes: 512,
      patch_action: 41,
      calls: 3,
      reason: null,
      created: '2026-11-21T09:00:00Z',
      modified: '2026-11-21T11:00:00Z',
      expires: null,
    },
  ],
  // The hub lists events newest first (ORDER BY id DESC).
  events: [
    { id: 3, at: '2026-11-21T12:00:00Z', kind: 'task_patch', agent: 'coder@node-1', detail: { task: 'k-2', action: 41, files: 2, bytes: 512 } },
    { id: 2, at: '2026-11-21T11:59:00Z', kind: 'agent_down', agent: 'coder@node-1', detail: { reason: 'stream_lost' } },
    { id: 1, at: '2026-11-21T11:58:00Z', kind: 'turn_started', agent: 'claude@node-1', detail: { turn: 't-9', kind: 'wake' } },
  ],
  problems: [],
  ...over,
});

const patchAction = (id: number, status: string): ActionItem => ({ id, thread_id: 'thread_abc123', tool: 'jev_mcp:apply_patch', status }) as ActionItem;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-21T12:00:30Z'));
  window.localStorage.setItem('guardian.project', 'server-guardian');
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('server-guardian'), project('fleet', 'ops'), project('arch-docs', 'design')],
    custom_dir: '/x',
  });
  mocked.fetchResidentMonitor.mockResolvedValue(snapshot());
  mocked.listActions.mockResolvedValue([]);
});

afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe('Resident agents tab', () => {
  it.each([
    ['code', 'server-guardian'],
    ['ops', 'fleet'],
    ['design', 'arch-docs'],
  ])('is offered for a %s project', async (_kind, id) => {
    window.localStorage.setItem('guardian.project', id);
    await mount('#/debates');
    expect(screen.getByRole('button', { name: D.page.tabResidents })).toBeTruthy();
  });

  it('shows the agents, the task board and the events of the snapshot', async () => {
    await mount('#/debates/residents');
    expect(screen.getByText(R.hubWide)).toBeTruthy();
    expect(screen.getAllByText(R.agentState.ready)).toHaveLength(1);
    expect(screen.getAllByText(R.agentState.down)).toHaveLength(1);
    expect(screen.getByText(R.toolsOpen)).toBeTruthy();
    expect(screen.getByText(R.notDeclared)).toBeTruthy();
    expect(screen.getByText(R.mailOpen(1, 0, 0))).toBeTruthy();

    // The task board: one column per status, the patch task links to Approvals with the real status.
    expect(screen.getByText('설명 보강')).toBeTruthy();
    expect(screen.getByText('테스트 추가')).toBeTruthy();
    const patch = screen.getByRole('link', { name: R.patchAction(41) });
    expect(patch.getAttribute('href')).toBe('#/approvals/41');
    // The action is not in the approvals list (another project, or not loaded): unknown, not a guess.
    expect(within(patch.closest('div')!).getByText(ko.common.unknown)).toBeTruthy();

    // The events feed reads newest first; detail shows as compact pairs.
    const rows = screen.getAllByRole('row');
    expect(rows[1].textContent).toContain('task_patch');
    expect(screen.getByText(/task=k-2 action=41 files=2 bytes=512/)).toBeTruthy();
  });

  it('joins the patch status from the approvals list when the action is known', async () => {
    mocked.listActions.mockResolvedValue([patchAction(41, 'PROPOSED')]);
    await mount('#/debates/residents');
    const patch = screen.getByRole('link', { name: R.patchAction(41) });
    expect(within(patch.closest('div')!).getByText(ko.agents.actionStatus.PROPOSED)).toBeTruthy();
  });

  it('opens an agent from a card, filters its mail and tasks, and loads a body on demand', async () => {
    mocked.fetchResidentMailBody.mockResolvedValue({ id: 'm-20261121-0001', body: '안녕하세요', bytes: 42, masked: true });
    await mount('#/debates/residents');
    follow(screen.getByRole('link', { name: R.openAgent('claude@node-1') }));
    await settle();
    expect(window.location.hash).toBe('#/debates/residents?agent=claude%40node-1');

    // The detail replaces the grid: its own header, its turns, only its mail, only its tasks.
    expect(screen.getByRole('link', { name: R.back }).getAttribute('href')).toBe('#/debates/residents');
    const turns = screen.getByRole('table', { name: R.turnsTitle });
    expect(within(turns).getByText('wake')).toBeTruthy();
    expect(within(turns).getByText('normal')).toBeTruthy();
    expect(within(turns).getByText(ko.common.running)).toBeTruthy();
    expect(within(turns).getByText(ko.debate.lib.durationMS(1, 0))).toBeTruthy();
    const mail = screen.getByRole('table', { name: R.mailTitle });
    expect(within(mail).getAllByRole('button', { name: R.showBody })).toHaveLength(2);
    expect(within(mail).getByText('상태 점검')).toBeTruthy();
    // coder@node-1 holds the completed task: only k-1 belongs to claude@node-1.
    const tasks = within(screen.getByRole('region', { name: R.tasksTitle }));
    expect(tasks.getByText('설명 보강')).toBeTruthy();
    expect(tasks.queryByText('테스트 추가')).toBeNull();

    // One body loads on demand, with the masking note.
    fireEvent.click(within(mail).getAllByRole('button', { name: R.showBody })[0]);
    await settle();
    expect(mocked.fetchResidentMailBody).toHaveBeenCalledWith('m-20261121-0001');
    expect(screen.getByText(/안녕하세요/)).toBeTruthy();
    expect(screen.getByText(/비밀값은 가린 상태로/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: R.hideBody }));
    expect(screen.queryByText(/안녕하세요/)).toBeNull();
  });

  it('shows a body failure inline and keeps the table usable', async () => {
    mocked.fetchResidentMailBody.mockRejectedValue(new api.HubError('mail gone', 404));
    window.location.hash = '#/debates/residents?agent=claude%40node-1';
    render(
      <GuardianRoot>
        <AppStores>
          <Routed />
        </AppStores>
      </GuardianRoot>,
    );
    await settle(300);
    fireEvent.click(screen.getAllByRole('button', { name: R.showBody })[0]);
    await settle();
    expect(screen.getByRole('alert').textContent).toContain('mail gone');
  });

  it('filters the events by agent and kind', async () => {
    await mount('#/debates/residents');
    const table = () => screen.getByRole('table', { name: R.eventsTitle });
    expect(table().querySelectorAll('tbody tr')).toHaveLength(3);
    fireEvent.change(screen.getByLabelText(R.filterAgent), { target: { value: 'claude@node-1' } });
    expect(table().querySelectorAll('tbody tr')).toHaveLength(1);
    expect(table().textContent).toContain('turn_started');
    fireEvent.change(screen.getByLabelText(R.filterKind), { target: { value: 'task_patch' } });
    // The two filters combine: claude@node-1 has no task_patch event, so the table gives way to
    // the empty line (an empty table renders no rows section at all).
    expect(screen.queryByRole('table', { name: R.eventsTitle })).toBeNull();
    expect(screen.getByText(R.noEvents)).toBeTruthy();
  });

  it('says so when the hub has no store or no agents, and lists the problems it found', async () => {
    mocked.fetchResidentMonitor.mockResolvedValue(
      snapshot({
        generated: null,
        store: { exists: false, schema: null, path_kind: null },
        agents: [],
        turns: {},
        mail: [],
        tasks: [],
        events: [],
        problems: ['events table missing'],
      }),
    );
    await mount('#/debates/residents');
    expect(screen.getByText(R.noStore)).toBeTruthy();
    expect(screen.getByText(R.noAgents)).toBeTruthy();
    expect(screen.getByText(/events table missing/)).toBeTruthy();
    expect(screen.queryByRole('region', { name: R.boardTitle })).toBeTruthy();
  });

  it('polls every 10 s while visible and stops while the tab is hidden', async () => {
    await mount('#/debates/residents');
    expect(mocked.fetchResidentMonitor).toHaveBeenCalledTimes(1);
    await settle(10_000);
    expect(mocked.fetchResidentMonitor).toHaveBeenCalledTimes(2);
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await settle(30_000);
    expect(mocked.fetchResidentMonitor).toHaveBeenCalledTimes(2);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await settle(0);
    expect(mocked.fetchResidentMonitor).toHaveBeenCalledTimes(3);
    Reflect.deleteProperty(document, 'visibilityState');
  });

  it('offers no write action anywhere: the refresh button is the only control', async () => {
    await mount('#/debates/residents');
    const buttons = screen.getAllByRole('button').map((b) => b.textContent);
    expect(buttons).toContain(ko.common.refresh);
    // The SegmentedControl tabs navigate; the tab itself has no POST/DELETE-backed control.
    expect(mocked.runAction).not.toHaveBeenCalled();
    expect(mocked.rejectAction).not.toHaveBeenCalled();
  });
});

describe('Resident agent not found', () => {
  it('warns and still shows the board when the address left the snapshot', async () => {
    window.location.hash = '#/debates/residents?agent=agy%40node-1';
    render(
      <GuardianRoot>
        <AppStores>
          <Routed />
        </AppStores>
      </GuardianRoot>,
    );
    await settle(300);
    expect(screen.getByText(R.agentMissing('agy@node-1'))).toBeTruthy();
    // The cards stay reachable.
    expect(screen.getByRole('link', { name: R.openAgent('claude@node-1') })).toBeTruthy();
  });
});

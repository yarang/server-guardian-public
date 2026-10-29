import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import { doneJob, runningJob, teamList } from './debateFixtures';
import { fleetSSH, groupList, keys, security, sshConfig } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { badgeOf } from '../src/App';
import { auditStatus, routeLabel } from '../src/lib/audit';
import { CommandHistory } from '../src/pages/run/CommandHistory';
import { groupKept, WorkspaceResetDialog } from '../src/pages/debate/WorkspaceResetDialog';
import { DebatesPage } from '../src/pages/DebatesPage';
import { SshPage } from '../src/pages/SshPage';
import { clearAuditCache } from '../src/lib/auditCache';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { HubStoreProvider } from '../src/state/hubStore';
import type { AuditEntry } from '../src/types';

const mocked = vi.mocked(api);
const originalMatchMedia = window.matchMedia;

function mockNarrow(narrow: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: narrow,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function Routed() {
  return <DebatesPage jobId={useRoute().params[0]} />;
}

const renderDebates = () =>
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );

afterEach(() => {
  cleanup();
  window.matchMedia = originalMatchMedia;
});

describe('published threads', () => {
  beforeEach(() => {
    mocked.fetchTeams.mockResolvedValue(teamList);
    mocked.listActions.mockResolvedValue([]);
    mocked.fetchPublications.mockResolvedValue([]);
    window.location.hash = `#/debates/${doneJob.id}`;
  });

  it('shows a Published chip on the list row and the result, and replaces "Continue this thread" with a reason', async () => {
    mocked.fetchDebates.mockResolvedValue([{ ...doneJob, thread_published: true }]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    renderDebates();
    const list = await screen.findByRole('navigation', { name: ko.agents.list.aria });
    expect(within(list).getByText(ko.agents.list.published)).toBeTruthy();
    const result = await screen.findByRole('region', { name: ko.debate.result.title });
    expect(within(result).getByText(ko.debate.job.published)).toBeTruthy();
    expect(within(result).queryByRole('button', { name: ko.debate.result.continueThread })).toBeNull();
    expect(within(result).getByText(/새 스레드로 시작/)).toBeTruthy();
  });

  it('keeps "Continue this thread" for an unpublished thread', async () => {
    mocked.fetchDebates.mockResolvedValue([{ ...doneJob, thread_published: false }]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    renderDebates();
    const result = await screen.findByRole('region', { name: ko.debate.result.title });
    expect(within(result).getByRole('button', { name: ko.debate.result.continueThread })).toBeTruthy();
    expect(within(result).queryByText(ko.debate.job.published)).toBeNull();
  });
});

describe('Debates on narrow screens', () => {
  beforeEach(() => {
    mocked.fetchTeams.mockResolvedValue(teamList);
    mocked.listActions.mockResolvedValue([]);
    mocked.fetchPublications.mockResolvedValue([]);
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
  });

  it('shows only the list at #/debates (no auto-selected detail)', async () => {
    mockNarrow(true);
    window.location.hash = '#/debates';
    renderDebates();
    expect(await screen.findByRole('navigation', { name: ko.agents.list.aria })).toBeTruthy();
    expect(screen.queryByRole('region', { name: ko.debate.result.title })).toBeNull();
    expect(screen.queryByRole('link', { name: ko.debate.page.backToList })).toBeNull();
    expect(mocked.fetchDebate).not.toHaveBeenCalled();
  });

  it('shows only the detail at #/debates/:id, with a Back to list link', async () => {
    mockNarrow(true);
    window.location.hash = `#/debates/${doneJob.id}`;
    renderDebates();
    expect(await screen.findByRole('region', { name: ko.debate.result.title })).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: ko.agents.list.aria })).toBeNull();
    expect(screen.getByRole('link', { name: ko.debate.page.backToList }).getAttribute('href')).toBe('#/debates');
  });

  it('keeps list and the newest detail side by side on wide screens', async () => {
    mockNarrow(false);
    window.location.hash = '#/debates';
    renderDebates();
    expect(await screen.findByRole('navigation', { name: ko.agents.list.aria })).toBeTruthy();
    expect(await screen.findByRole('region', { name: ko.debate.result.title })).toBeTruthy();
    expect(screen.queryByRole('link', { name: ko.debate.page.backToList })).toBeNull();
  });
});

describe('Debates list is fetched once for the page and Overview', () => {
  it('uses the store list (one request on open) instead of its own', async () => {
    mocked.fetchTeams.mockResolvedValue(teamList);
    mocked.listActions.mockResolvedValue([]);
    mocked.fetchDebates.mockReset();
    mocked.fetchDebates.mockResolvedValue([runningJob]);
    mocked.fetchDebate.mockResolvedValue({ ...doneJob, ...runningJob, log: '', result: null });
    window.location.hash = '#/debates';
    renderDebates();
    await screen.findByRole('navigation', { name: ko.agents.list.aria });
    expect(mocked.fetchDebates).toHaveBeenCalledTimes(1);
  });
});

describe('navigation badges', () => {
  it('keeps the approvals count visible next to Running', () => {
    expect(badgeOf(3, true)).toEqual({ badge: `3 · ${ko.common.running}`, badgeTone: 'crit' });
    expect(badgeOf(3, false)).toEqual({ badge: '3', badgeTone: 'crit' });
    expect(badgeOf(0, true)).toEqual({ badge: ko.common.running, badgeTone: 'info' });
  });
});

describe('Reset workspace: release threads', () => {
  const plan = {
    ok: true,
    project_id: 'svc',
    workspace: '/ws/svc',
    discard: [{ path: 'a.go', state: 'modified' }],
    keep: [
      { path: 'b.go', action_id: 41, thread_id: 'thread_one' },
      { path: 'c.go', action_id: 42, thread_id: 'thread_one' },
      { path: 'd.go', action_id: 50, thread_id: 'thread_two' },
    ],
  };
  const open = async () => {
    mocked.getWorkspaceReset.mockResolvedValue(plan);
    mocked.resetWorkspace.mockReset();
    mocked.resetWorkspace.mockResolvedValue({ ok: true, discarded: ['a.go', 'b.go'] });
    render(
      <GuardianRoot>
        <WorkspaceResetDialog projectId="svc" onClose={() => undefined} onDone={() => undefined} />
      </GuardianRoot>
    );
    return screen.findByRole('dialog', { name: ko.debate.reset.title('svc') });
  };

  it('groups kept files by thread', () => {
    expect(groupKept(plan.keep).map((g) => [g.thread, g.files.map((f) => f.path)])).toEqual([
      ['thread_one', ['b.go', 'c.go']],
      ['thread_two', ['d.go']],
    ]);
    expect(groupKept([{ path: 'x', action_id: 1 }])[0].thread).toBe('');
  });

  it('lists each thread with a danger-toned Release checkbox and sends only the ticked ones', async () => {
    const d = await open();
    const one = await within(d).findByTestId('kept-thread_one');
    expect(within(one).getByText(`b.go (${ko.debate.reset.actionRef(41)})`)).toBeTruthy();
    expect(within(one).getByText(/더 이상 게시할 수 없어요/)).toBeTruthy();
    expect(within(d).getByTestId('kept-thread_two')).toBeTruthy();
    fireEvent.click(within(one).getByLabelText(new RegExp(ko.debate.reset.release('thread_one'))));
    fireEvent.click(within(d).getByRole('button', { name: ko.debate.project.resetWorkspace }));
    await waitFor(() => expect(mocked.resetWorkspace).toHaveBeenCalledWith('svc', ['thread_one']));
  });

  it('sends no release list when nothing is ticked', async () => {
    const d = await open();
    await within(d).findByTestId('kept-thread_one');
    fireEvent.click(within(d).getByRole('button', { name: ko.debate.project.resetWorkspace }));
    await waitFor(() => expect(mocked.resetWorkspace).toHaveBeenCalledWith('svc'));
  });
});

describe('SSH audit tab uses the reachability class', () => {
  it('names a host key change instead of "unreachable"', async () => {
    clearAuditCache();
    mocked.fetchFleetSSHAudit.mockResolvedValue({
      ...fleetSSH,
      nodes: fleetSSH.nodes.map((n) => (n.id === 'c3' ? { ...n, reachability: 'host_key_changed' as const } : n)),
    });
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.fetchSSHSecurity.mockResolvedValue(security);
    mocked.fetchSSHConfig.mockResolvedValue(sshConfig);
    mocked.fetchAuthorizedKeys.mockResolvedValue(keys);
    render(
      <GuardianRoot>
        <HubStoreProvider>
          <SshPage />
        </HubStoreProvider>
      </GuardianRoot>
    );
    expect(await screen.findByText(ko.fleet.ssh.reachabilityCount(ko.shell.reachability.hostKeyChanged, 1))).toBeTruthy();
  });
});

describe('Command history: new routes, detail and rejected', () => {
  const entries: AuditEntry[] = [
    { ts: '2026-10-10T09:00:00Z', actor: 'session@10.0.0.9', route: '/api/nodes/a1/revoke-key', targets: ['a1'], command: '', status: 'ok' },
    { ts: '2026-10-10T08:00:00Z', actor: 'token@10.0.0.9', route: '/api/nodes/a1', targets: ['a1'], command: '', status: 'rejected', detail: 'node is the hub' },
    { ts: '2026-10-10T07:00:00Z', route: 'fleet', targets: ['a1', 'b2'], command: 'uptime', exit_codes: { a1: 0, b2: 1 }, status: 'error' },
  ];

  it('maps routes and statuses', () => {
    expect(routeLabel('/api/nodes/a1/revoke-key')).toBe(ko.shell.audit.route.revokeKey);
    expect(routeLabel('/api/nodes/a1')).toBe(ko.shell.audit.route.node);
    expect(routeLabel('fleet')).toBe(ko.shell.audit.route.fleet);
    expect(auditStatus('rejected')).toEqual({ tone: 'warn', label: ko.shell.audit.status.rejected });
    expect(auditStatus('error').label).toBe(ko.shell.audit.status.error);
    expect(auditStatus('ok').tone).toBe('ok');
  });

  it('shows the detail, a rejected chip and no exit code for non-command entries', async () => {
    mocked.fetchAudit.mockResolvedValue(entries);
    render(
      <GuardianRoot>
        <CommandHistory refreshKey="x" />
      </GuardianRoot>
    );
    const table = await screen.findByRole('table', { name: ko.ops.run.history.label });
    expect(within(table).getByText(ko.shell.audit.route.revokeKey)).toBeTruthy();
    expect(within(table).getByText(ko.shell.audit.route.node)).toBeTruthy();
    expect(within(table).getByText(ko.shell.audit.status.rejected)).toBeTruthy();
    expect(within(table).getByText('node is the hub')).toBeTruthy();
    expect(within(table).getByText(ko.shell.audit.failedOf(1, 2))).toBeTruthy();
  });
});

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { action } from './debateFixtures';
import { ko } from '../src/i18n/ko';
import { diagnosis, fleetSSH, groupList, metrics, security, servers, sshConfig, keys } from './fixtures';
import type { ActionItem, ActionRunResponse } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { App } from '../src/App';
import { exitSummary, routeLabel } from '../src/lib/audit';
import { ApprovalsPage } from '../src/pages/ApprovalsPage';
import { NodeDetailPage } from '../src/pages/NodeDetailPage';
import { RunPage } from '../src/pages/RunPage';
import { SettingsPage } from '../src/pages/SettingsPage';
import { ActionDialog } from '../src/pages/debate/ActionDialog';
import { AppStores } from '../src/state/AppStores';
import { HubStoreProvider } from '../src/state/hubStore';
import { OperationsStoreProvider, useOperationsStore } from '../src/state/operationsStore';
import { RunStoreProvider } from '../src/state/runStore';

const mocked = vi.mocked(api);
const flush = () => act(async () => undefined);

// --- D5: revoke the hub key on a node -------------------------------------------------------------

describe('D5: Revoke hub key', () => {
  const node = { ...servers[0], connection: 'fleet_file' as const, host_key_mode: 'pinned' as const };
  const preview = { node: 'a1', target: 'u@10.0.0.1:22', fingerprint: 'SHA256:abc', match: 'exactly one key matches', note: 'The hub is the only client of this key.' };

  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue([node]);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
    mocked.fetchRevokeKeyPreview.mockReset();
    mocked.revokeNodeKey.mockReset();
    mocked.fetchRevokeKeyPreview.mockResolvedValue(preview);
  });

  async function openRevoke() {
    render(
      <GuardianRoot>
        <HubStoreProvider>
          <NodeDetailPage id="a1" />
        </HubStoreProvider>
      </GuardianRoot>
    );
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.revokeKey.revoke }));
    return screen.findByRole('dialog', { name: new RegExp('^' + ko.fleet.revokeKey.title('')) });
  }
  const confirmButton = (d: HTMLElement) => within(d).getByRole('button', { name: ko.fleet.revokeKey.revoke }) as HTMLButtonElement;

  it('previews the key, requires the node id, then revokes and points to Remove node', async () => {
    mocked.revokeNodeKey.mockResolvedValue({ ok: true, removed: 1, note: 'one line removed' });
    const d = await openRevoke();
    expect(await within(d).findByText('u@10.0.0.1:22')).toBeTruthy();
    expect(within(d).getByText('SHA256:abc')).toBeTruthy();
    expect(within(d).getByText('exactly one key matches')).toBeTruthy();
    expect(within(d).getByText(ko.fleet.revokeKey.warning)).toBeTruthy();
    expect(confirmButton(d).disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a' } });
    expect(confirmButton(d).disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    expect(confirmButton(d).disabled).toBe(false);
    fireEvent.click(confirmButton(d));
    expect(await within(d).findByText(new RegExp(ko.fleet.revokeKey.removed(1, 'a1').replace('.', '\\.')))).toBeTruthy();
    expect(within(d).getByText(/one line removed/)).toBeTruthy();
    expect(within(d).getByText(ko.fleet.removeNode.remove)).toBeTruthy();
    expect(mocked.revokeNodeKey).toHaveBeenCalledWith('a1');
  });

  it('shows the hub error with its reachability detail and keeps the dialog usable', async () => {
    mocked.revokeNodeKey.mockRejectedValue(new api.HubError('ssh failed', 502, { reachability: 'connect timed out' }));
    const d = await openRevoke();
    await within(d).findByText('u@10.0.0.1:22');
    fireEvent.change(within(d).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    fireEvent.click(confirmButton(d));
    const alert = await within(d).findByRole('alert');
    expect(alert.textContent).toContain('ssh failed');
    expect(alert.textContent).toContain('connect timed out');
    expect(confirmButton(d).disabled).toBe(false);
  });

  it('cannot revoke when the preview fails', async () => {
    mocked.fetchRevokeKeyPreview.mockRejectedValue(new Error('no such key'));
    const d = await openRevoke();
    expect(await within(d).findByText('no such key')).toBeTruthy();
    fireEvent.change(within(d).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    expect(confirmButton(d).disabled).toBe(true);
  });
});

// --- D6 + D8: Commands page ---------------------------------------------------------------------

describe('D6: command history', () => {
  it('maps routes and exit codes to short labels', () => {
    expect(['local', 'node', 'group', 'fleet', 'weird'].map(routeLabel)).toEqual([ko.shell.audit.route.local, ko.shell.audit.route.node, ko.shell.audit.route.group, ko.shell.audit.route.fleet, 'weird']);
    expect(exitSummary({ exit_codes: { a: 0 } })).toBe('exit 0');
    expect(exitSummary({ exit_codes: { a: 0, b: 0 } })).toBe(ko.shell.audit.allExitOk(2));
    expect(exitSummary({ exit_codes: { a: 0, b: 255, c: 1 } })).toBe(ko.shell.audit.failedOf(2, 3));
    expect(exitSummary({ exit_codes: { a: 3 } })).toBe('exit 3');
    expect(exitSummary({})).toBe('—');
  });
});

function mountRun() {
  return render(
    <GuardianRoot>
      <HubStoreProvider>
        <RunStoreProvider>
          <RunPage />
        </RunStoreProvider>
      </HubStoreProvider>
    </GuardianRoot>
  );
}

describe('Commands page', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.streamFleetExec.mockReset();
    mocked.streamFleetExec.mockImplementation(() => () => undefined);
    mocked.fetchAudit.mockReset();
    mocked.fetchAudit.mockResolvedValue([]);
  });

  it('lists recent audit entries with route, targets, command and result', async () => {
    mocked.fetchAudit.mockResolvedValue([
      { ts: '2026-10-05T03:00:00Z', actor: 'op', route: 'fleet', targets: ['a1', 'b2'], command: 'uptime', exit_codes: { a1: 0, b2: 255 }, duration_ms: 90, status: 'error' },
      { ts: '2026-10-05T02:00:00Z', route: 'local', targets: ['local'], command: 'df -h /', exit_codes: { local: 0 }, status: 'ok' },
    ]);
    mountRun();
    const table = await screen.findByRole('table', { name: ko.ops.run.history.label });
    expect(mocked.fetchAudit).toHaveBeenCalledWith(20);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText(ko.shell.audit.route.fleet)).toBeTruthy();
    expect(within(rows[0]).getByText(ko.ops.run.history.targetsCount(2))).toBeTruthy();
    expect(within(rows[0]).getByText('uptime')).toBeTruthy();
    expect(within(rows[0]).getByText(ko.shell.audit.status.error)).toBeTruthy();
    expect(within(rows[0]).getByText(ko.shell.audit.failedOf(1, 2) )).toBeTruthy();
    expect(rows[0].querySelector('[title="a1, b2"]')).toBeTruthy();
    expect(within(rows[1]).getByText(ko.shell.audit.route.local)).toBeTruthy();
    expect(within(rows[1]).getByText(ko.shell.audit.status.ok)).toBeTruthy();
    expect(within(rows[1]).getByText('exit 0')).toBeTruthy();
  });

  it('shows the empty and the error state of the history', async () => {
    mountRun();
    expect(await screen.findByText(ko.ops.run.history.empty)).toBeTruthy();
  });

  it('shows a history failure with a retry', async () => {
    mocked.fetchAudit.mockRejectedValue(new Error('audit down'));
    mountRun();
    expect(await screen.findByText('audit down')).toBeTruthy();
    expect(screen.getByText(ko.common.couldNotLoad(ko.ops.run.history.what))).toBeTruthy();
  });
});

describe('D8: one confirmation for group and fleet runs', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.streamFleetExec.mockReset();
    mocked.streamFleetExec.mockImplementation(() => () => undefined);
    mocked.executeRemoteSSH.mockReset();
    mocked.executeRemoteSSH.mockResolvedValue({ exit_code: 0, stdout: 'x', stderr: '', duration_ms: 1 });
  });

  const pick = (value: string) => fireEvent.change(screen.getByLabelText(ko.ops.run.target), { target: { value } });

  it('lists every server by name and the exact command before a fleet run, not typed', async () => {
    mountRun();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    pick('all');
    const input = screen.getByLabelText(ko.shell.ui.terminal.command) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'systemctl restart nginx' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    const d = screen.getByRole('dialog', { name: ko.ops.run.confirm.titleFleet });
    expect(within(d).getByLabelText(ko.ops.run.confirm.nodesAria).textContent).toBe('site-a-arm1, site-b-ec1');
    expect(within(d).getByText('systemctl restart nginx')).toBeTruthy();
    expect(within(d).queryByRole('textbox')).toBeNull();
    fireEvent.click(within(d).getByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
    expect(mocked.streamFleetExec).toHaveBeenCalledWith('systemctl restart nginx', [], expect.any(Function), expect.any(Function), 15);
  });

  it('a preset on the fleet goes through the same dialog; a preset on one node runs at once', async () => {
    mountRun();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    pick('all');
    fireEvent.click(screen.getByRole('button', { name: 'uptime' }));
    const d = screen.getByRole('dialog', { name: ko.ops.run.confirm.titleFleet });
    expect(within(d).getByText('uptime')).toBeTruthy();
    fireEvent.click(within(d).getByRole('button', { name: ko.shell.confirm.cancel }));
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();

    pick('b2');
    fireEvent.click(screen.getByRole('button', { name: 'uptime' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocked.executeRemoteSSH).toHaveBeenCalledWith('b2', 'uptime');
  });
});

// --- D10: CLOSED badge --------------------------------------------------------------------------

describe('D10: closed threads on actions', () => {
  beforeEach(() => {
    for (const fn of [mocked.listActions, mocked.getAction, mocked.fetchClosedThreads]) fn.mockReset();
    mocked.fetchProjects.mockResolvedValue({ projects: [], custom_dir: '/x' });
    mocked.fetchDebates.mockResolvedValue([]);
    mocked.listActions.mockResolvedValue([
      action({ id: 1, thread_id: 'thread_closed', reason: 'in closed' }),
      action({ id: 2, thread_id: 'thread_open', reason: 'in open' }),
    ]);
    mocked.fetchClosedThreads.mockResolvedValue(['thread_closed']);
    window.location.hash = '#/approvals';
  });
  const mountApprovals = (id?: string) =>
    render(
      <GuardianRoot>
        <AppStores>
          <ApprovalsPage actionId={id} />
        </AppStores>
      </GuardianRoot>
    );

  it('marks only the actions of a closed thread', async () => {
    mountApprovals();
    const table = await screen.findByRole('table', { name: ko.debate.actions.title });
    await waitFor(() => expect(within(table).getAllByText(ko.debate.actions.threadClosed)).toHaveLength(1));
    const rows = within(table).getAllByRole('row').slice(1);
    const closedRow = rows.find((r) => r.textContent?.includes('in closed'))!;
    const openRow = rows.find((r) => r.textContent?.includes('in open'))!;
    expect(within(closedRow).getByText(ko.debate.actions.threadClosed)).toBeTruthy();
    expect(within(openRow).queryByText(ko.debate.actions.threadClosed)).toBeNull();
  });

  it('still lists the actions when the hub has no closed-thread list', async () => {
    mocked.fetchClosedThreads.mockRejectedValue(new api.HubError('missing', 404));
    mountApprovals();
    const table = await screen.findByRole('table', { name: ko.debate.actions.title });
    expect(within(table).getByText('in closed')).toBeTruthy();
    expect(within(table).queryByText(ko.debate.actions.threadClosed)).toBeNull();
  });

  it('says in the detail that closing a thread does not affect its actions', async () => {
    mocked.getAction.mockResolvedValue(action({ id: 1, thread_id: 'thread_closed' }));
    mountApprovals('1');
    const d = await screen.findByRole('dialog', { name: ko.debate.dialog.title(1) });
    await waitFor(() => expect(within(d).getByText(ko.debate.actions.threadClosed)).toBeTruthy());
    expect(within(d).getByText(ko.debate.dialog.threadClosedNote)).toBeTruthy();
  });
});

// --- D12: workspace reset -----------------------------------------------------------------------

describe('D12: workspace reset', () => {
  const projects = {
    custom_dir: '/x',
    projects: [
      {
        id: 'svc',
        source: 'custom' as const,
        data: { id: 'svc', name: 'My Service', repo: 'https://git.example/svc.git' },
        errors: [],
        status: { workspace: '/ws/svc', present: true, head: 'abcdef1234567890', branch: 'main', modified: 2 },
      },
    ],
  };
  const plan = {
    ok: true,
    project_id: 'svc',
    workspace: '/ws/svc',
    discard: [{ path: 'a.go', state: 'modified' }, { path: 'notes.txt', state: 'untracked' }],
    keep: [{ path: 'b.go', action_id: 41 }],
  };

  beforeEach(() => {
    for (const fn of [mocked.fetchProjects, mocked.syncProject, mocked.getWorkspaceReset, mocked.resetWorkspace]) fn.mockReset();
    mocked.fetchProjects.mockResolvedValue(projects);
    mocked.getWorkspaceReset.mockResolvedValue(plan);
    mocked.fetchDebates.mockResolvedValue([]);
    mocked.listActions.mockResolvedValue([]);
    mocked.fetchTeams.mockResolvedValue({ teams: [], custom_dir: '/x' } as never);
  });
  const mountSettings = () =>
    render(
      <GuardianRoot>
        <AppStores>
          <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={() => undefined} section="projects" />
        </AppStores>
      </GuardianRoot>
    );

  it('shows the plan, confirms, resets and lists what was discarded', async () => {
    mocked.resetWorkspace.mockResolvedValue({ ok: true, discarded: ['a.go', 'notes.txt'] });
    mountSettings();
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.project.resetAria('svc') }));
    const d = await screen.findByRole('dialog', { name: ko.debate.reset.title('svc') });
    expect(await within(d).findByText('a.go (modified)')).toBeTruthy();
    expect(within(d).getByText('notes.txt (untracked)')).toBeTruthy();
    expect(within(d).getByText(`b.go (${ko.debate.reset.actionRef(41)})`)).toBeTruthy();
    expect(mocked.resetWorkspace).not.toHaveBeenCalled();
    const confirm = within(d).getByRole('button', { name: ko.debate.project.resetWorkspace });
    expect(confirm.className).toContain('danger');
    fireEvent.click(confirm);
    const done = await screen.findByRole('dialog', { name: ko.debate.reset.doneTitle('svc') });
    expect(within(done).getByText(ko.debate.reset.discarded(2))).toBeTruthy();
    expect(within(done).getByText('notes.txt')).toBeTruthy();
    expect(mocked.resetWorkspace).toHaveBeenCalledWith('svc');
  });

  it('explains 409 (busy) and 403 (UI actions off) clearly', async () => {
    mocked.resetWorkspace.mockRejectedValueOnce(new api.HubError('workspace busy', 409));
    mountSettings();
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.project.resetAria('svc') }));
    const d = await screen.findByRole('dialog', { name: ko.debate.reset.title('svc') });
    await within(d).findByText('a.go (modified)');
    fireEvent.click(within(d).getByRole('button', { name: ko.debate.project.resetWorkspace }));
    expect((await within(d).findByRole('alert')).textContent).toContain(ko.debate.reset.busy('x').split('(')[0].trim());
    mocked.resetWorkspace.mockRejectedValueOnce(new api.HubError('UI actions are disabled', 403));
    fireEvent.click(within(d).getByRole('button', { name: ko.debate.project.resetWorkspace }));
    await waitFor(() => expect(within(d).getByRole('alert').textContent).toContain(ko.debate.reset.uiOff));
  });

  it('is offered from a skipped sync result', async () => {
    mocked.syncProject.mockResolvedValue({ id: 'svc', ok: true, action: 'skipped', reason: 'workspace has local changes' });
    mountSettings();
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.project.syncAria('svc') }));
    expect(await screen.findByText(ko.debate.project.notUpdated('svc', 'workspace has local changes'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.project.resetFromSync('svc') }));
    expect(await screen.findByRole('dialog', { name: ko.debate.reset.title('svc') })).toBeTruthy();
    expect(mocked.getWorkspaceReset).toHaveBeenCalledWith('svc');
  });
});

// --- D13: long-running operations survive leaving the page -----------------------------------------

describe('D13: operations store', () => {
  function Probe() {
    const ops = useOperationsStore();
    return (
      <div>
        <span data-testid="exec">{ops.execs[7]?.phase ?? 'none'}</span>
        <span data-testid="pub">{ops.publishes.thread_abc123?.phase ?? 'none'}</span>
        <span data-testid="flags">{`${ops.execRunning}/${ops.publishRunning}`}</span>
        <button onClick={() => ops.startExec(action({ id: 7 }))}>exec</button>
        <button onClick={() => ops.startPublish('thread_abc123', ' feature/x ')}>pub</button>
      </div>
    );
  }
  const settled = vi.fn();

  beforeEach(() => {
    mocked.runAction.mockReset();
    mocked.publishThread.mockReset();
    settled.mockReset();
  });

  const mountProbe = () =>
    render(
      <GuardianRoot>
        <OperationsStoreProvider onSettled={settled}>
          <Probe />
        </OperationsStoreProvider>
      </GuardianRoot>
    );

  it('tracks a run, keeps the final result and notifies when it settles; a second click does not start another', async () => {
    let finish: (v: ActionRunResponse) => void = () => undefined;
    mocked.runAction.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    mountProbe();
    fireEvent.click(screen.getByText('exec'));
    fireEvent.click(screen.getByText('exec'));
    expect(screen.getByTestId('exec').textContent).toBe('running');
    expect(screen.getByTestId('flags').textContent).toBe('true/false');
    expect(mocked.runAction).toHaveBeenCalledTimes(1);
    finish({ ok: true, action: action({ id: 7, status: 'EXECUTED' }) });
    await flush();
    expect(screen.getByTestId('exec').textContent).toBe('done');
    expect(screen.getByTestId('flags').textContent).toBe('false/false');
    expect(settled).toHaveBeenCalledTimes(1);
  });

  it('keeps a failed run as an error with the hub wording', async () => {
    mocked.runAction.mockRejectedValue(new api.HubError('workspace moved', 409));
    mountProbe();
    fireEvent.click(screen.getByText('exec'));
    await flush();
    expect(screen.getByTestId('exec').textContent).toBe('error');
  });

  it('publishes with the trimmed branch and tracks the in-flight flag', async () => {
    let finish: (v: never) => void = () => undefined;
    mocked.publishThread.mockReturnValue(new Promise((resolve) => (finish = resolve as never)));
    mountProbe();
    fireEvent.click(screen.getByText('pub'));
    expect(screen.getByTestId('flags').textContent).toBe('false/true');
    expect(mocked.publishThread).toHaveBeenCalledWith('thread_abc123', 'feature/x');
    finish({ ok: true, status: 'PUSHED', branch: 'feature/x' } as never);
    await flush();
    expect(screen.getByTestId('pub').textContent).toBe('done');
  });

  it('shows the finished result of an Execute when the dialog is opened again after leaving', async () => {
    let finish: (v: ActionRunResponse) => void = () => undefined;
    mocked.getAction.mockResolvedValue(action({ id: 57 }));
    mocked.runAction.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const onClose = vi.fn();
    function Host() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button onClick={() => setOpen((o) => !o)}>toggle</button>
          {open ? <ActionDialog id={57} onClose={onClose} onChanged={() => undefined} /> : <span>elsewhere</span>}
        </>
      );
    }
    render(
      <GuardianRoot>
        <OperationsStoreProvider>
          <Host />
        </OperationsStoreProvider>
      </GuardianRoot>
    );
    await screen.findByRole('dialog', { name: ko.debate.dialog.title(57) });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.debate.dialog.execute }));
    fireEvent.click(screen.getByLabelText(ko.debate.dialog.reviewed));
    fireEvent.click(screen.getByRole('button', { name: ko.debate.dialog.executeAction(57) }));
    await flush();
    fireEvent.click(screen.getByText('toggle')); // leave while running
    expect(screen.getByText('elsewhere')).toBeTruthy();
    const done: ActionItem = action({ id: 57, status: 'EXECUTED', result: { exit_code: 0, stdout: 'all good' } });
    finish({ ok: true, action: done });
    await flush();
    fireEvent.click(screen.getByText('toggle')); // come back
    expect(await screen.findByText(ko.debate.dialog.finished(57, 'EXECUTED'))).toBeTruthy();
    expect(screen.getByText('all good')).toBeTruthy();
    expect(screen.getByText(ko.debate.dialog.resultNow)).toBeTruthy();
  });
});

describe('D13: nav indicator', () => {
  it('shows Running on Approvals while an Execute is in flight on another page', async () => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchSSHSecurity.mockResolvedValue(security);
    mocked.fetchSSHConfig.mockResolvedValue(sshConfig);
    mocked.fetchAuthorizedKeys.mockResolvedValue(keys);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchTeams.mockResolvedValue({ teams: [], custom_dir: '/x' } as never);
    mocked.fetchDebates.mockResolvedValue([]);
    mocked.listActions.mockResolvedValue([]);
    mocked.getAction.mockResolvedValue(action({ id: 57 }));
    mocked.runAction.mockReturnValue(new Promise(() => undefined));
    window.location.hash = '#/approvals/57';
    render(
      <GuardianRoot>
        <App />
      </GuardianRoot>
    );
    await screen.findByRole('dialog', { name: ko.debate.dialog.title(57) });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.debate.dialog.execute }));
    fireEvent.click(screen.getByLabelText(ko.debate.dialog.reviewed));
    fireEvent.click(screen.getByRole('button', { name: ko.debate.dialog.executeAction(57) }));
    await flush();
    act(() => {
      window.location.hash = '#/fleet';
    });
    const nav = screen.getByRole('navigation', { name: ko.shell.nav.primary });
    await waitFor(() => expect(within(nav).getByRole('link', { name: `${ko.shell.nav.routes.approvals.label} ${ko.common.running}` })).toBeTruthy());
    window.location.hash = '';
  });
});

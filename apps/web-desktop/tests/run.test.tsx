import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { groupList, servers } from './fixtures';
import type { FleetStreamEvent } from '../src/types';
import { ko } from '../src/i18n/ko';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { RunPage } from '../src/pages/RunPage';
import { HubStoreProvider } from '../src/state/hubStore';
import { RunStoreProvider, useRunStore } from '../src/state/runStore';

const mocked = vi.mocked(api);

let emit: (e: FleetStreamEvent) => void;
let fail: (err: unknown) => void;
const cancel = vi.fn();

function run(command: string) {
  const input = screen.getByLabelText(ko.shell.ui.terminal.command) as HTMLInputElement;
  fireEvent.change(input, { target: { value: command } });
  fireEvent.submit(input.closest('form') as HTMLFormElement);
}

const SITE_B_NODE = 'site-b-ec1 (10.0.0.2)';
const target = () => (screen.getByLabelText(ko.ops.run.target) as HTMLSelectElement).value;
const pick = (value: string) => fireEvent.change(screen.getByLabelText(ko.ops.run.target), { target: { value } });

/** Mounts the stores and a page that can be toggled away, like a route change in the app. */
function Harness({ urlTarget }: { urlTarget?: string }) {
  const [away, setAway] = useState(false);
  const { multiRunning } = useRunStore();
  return (
    <>
      <button onClick={() => setAway((a) => !a)}>toggle page</button>
      <span data-testid="running-flag">{multiRunning ? 'yes' : 'no'}</span>
      {away ? <div>elsewhere</div> : <RunPage target={urlTarget} />}
    </>
  );
}

const mount = (urlTarget?: string) =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <RunStoreProvider>
          <Harness urlTarget={urlTarget} />
        </RunStoreProvider>
      </HubStoreProvider>
    </GuardianRoot>
  );

const leave = () => fireEvent.click(screen.getByRole('button', { name: 'toggle page' }));

async function startFleetRun(command = 'uptime') {
  await screen.findByRole('option', { name: SITE_B_NODE });
  pick('all');
  run(command);
  fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
}

beforeEach(() => {
  cancel.mockReset();
  mocked.fetchServers.mockResolvedValue(servers);
  mocked.fetchGroups.mockResolvedValue(groupList);
  mocked.streamFleetExec.mockImplementation((_cmd, _targets, onEvent, onError) => {
    emit = (e) => act(() => onEvent(e));
    fail = (err) => act(() => onError(err));
    return cancel;
  });
});

describe('Run: single targets', () => {
  it('defaults to the local agent node and shows stdout and exit 0', async () => {
    mocked.executeCommand.mockResolvedValue({ exit_code: 0, stdout: 'up 3 days\n', stderr: '', duration_ms: 12 });
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    expect(target()).toBe('local');
    run('uptime');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(await screen.findByText('up 3 days')).toBeTruthy();
    expect(screen.getByText(`[${ko.ops.run.labelLocal}] uptime`)).toBeTruthy();
    expect(screen.getByText('exit 0 · 12 ms')).toBeTruthy();
    expect(mocked.executeCommand).toHaveBeenCalledWith('uptime');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
  });

  it('lists local, nodes, groups (empty ones disabled) and the fleet', async () => {
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    await screen.findByRole('option', { name: ko.ops.run.targetGroup('Site B', 1) });
    const options = within(screen.getByLabelText(ko.ops.run.target)).getAllByRole('option') as HTMLOptionElement[];
    expect(options[0].textContent).toBe(ko.ops.run.targetHub);
    expect(options[options.length - 1].textContent).toBe(ko.ops.run.targetAll);
    expect(options.find((o) => o.textContent === ko.ops.run.targetGroup('Empty', 0))?.disabled).toBe(true);
  });

  it('shows "Command was not run" with exit -1 when the request fails, never success', async () => {
    mocked.executeRemoteSSH.mockResolvedValue({
      exit_code: -1,
      stdout: '',
      stderr: 'Command was not run: Cannot reach the hub: timeout',
      duration_ms: 0,
    });
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    pick('b2');
    run('systemctl status sshd');

    expect(await screen.findByText('Command was not run: Cannot reach the hub: timeout')).toBeTruthy();
    expect(screen.getByText(/exit -1/)).toBeTruthy();
    expect(screen.queryByText(/exit 0/)).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocked.executeRemoteSSH).toHaveBeenCalledWith('b2', 'systemctl status sshd');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText(ko.ops.run.running)).toBeNull());
    // Single targets keep the terminal view: no per-node table.
    expect(screen.queryByRole('table', { name: ko.ops.run.results.title })).toBeNull();
  });

  it('shows stderr and a non-zero exit code as an error', async () => {
    mocked.executeCommand.mockResolvedValue({ exit_code: 127, stdout: '', stderr: 'nope: command not found', duration_ms: 3 });
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    run('nope');
    expect(await screen.findByText('nope: command not found')).toBeTruthy();
    expect(screen.getByText('exit 127 · 3 ms')).toBeTruthy();
  });

  it('accumulates history across runs and targets, prefixed with the target label', async () => {
    mocked.executeCommand.mockResolvedValue({ exit_code: 0, stdout: 'local-out\n', stderr: '', duration_ms: 1 });
    mocked.executeRemoteSSH.mockResolvedValue({ exit_code: 0, stdout: 'remote-out\n', stderr: '', duration_ms: 2 });
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    run('first');
    await screen.findByText('local-out');
    pick('b2');
    run('second');
    await screen.findByText('remote-out');
    expect(screen.getByText(`[${ko.ops.run.labelLocal}] first`)).toBeTruthy();
    expect(screen.getByText('local-out')).toBeTruthy();
    expect(screen.getByText('[site-b-ec1] second')).toBeTruthy();
  });

  it('keeps the history when the page is left and reopened, and ignores a result that lands in between', async () => {
    let finish: (r: { exit_code: number; stdout: string; stderr: string; duration_ms: number }) => void = () => undefined;
    mocked.executeCommand.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    run('slow');
    await screen.findByText(ko.ops.run.running);
    leave();
    expect(screen.getByText('elsewhere')).toBeTruthy();
    await act(async () => finish({ exit_code: 0, stdout: 'done-while-away\n', stderr: '', duration_ms: 5 }));
    leave();
    expect(screen.getByText(`[${ko.ops.run.labelLocal}] slow`)).toBeTruthy();
    expect(screen.getByText('done-while-away')).toBeTruthy();
    expect(screen.queryByText(ko.ops.run.running)).toBeNull();
  });
});

describe('Run: fleet and groups', () => {
  it('sends the chosen per-node timeout, clamped to 1-600 s, and defaults to 15 s', async () => {
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    expect(screen.queryByLabelText(ko.ops.run.timeoutPerNode)).toBeNull();
    pick('all');
    const field = screen.getByLabelText(ko.ops.run.timeoutPerNode) as HTMLInputElement;
    expect(field.value).toBe('15');
    fireEvent.change(field, { target: { value: '9999' } });
    run('uptime');
    fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
    expect(mocked.streamFleetExec).toHaveBeenLastCalledWith('uptime', [], expect.any(Function), expect.any(Function), 600);
  });

  it('parseFleetTimeout falls back to 15 s for blank or invalid input and clamps the rest', async () => {
    const { parseFleetTimeout } = await import('../src/pages/RunPage');
    expect(parseFleetTimeout('')).toBe(15);
    expect(parseFleetTimeout('abc')).toBe(15);
    expect(parseFleetTimeout('0')).toBe(1);
    expect(parseFleetTimeout('-5')).toBe(1);
    expect(parseFleetTimeout('45')).toBe(45);
    expect(parseFleetTimeout('601')).toBe(600);
  });

  it('asks for confirmation before a fleet-wide run, then streams per-node results', async () => {
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    pick('all');
    run('uptime');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
    expect(mocked.streamFleetExec).toHaveBeenCalledWith('uptime', [], expect.any(Function), expect.any(Function), 15);

    emit({ type: 'start', command: 'uptime', total: 2 });
    emit({ type: 'node_start', node: servers[0] });
    emit({
      type: 'node_done',
      completed: 1,
      result: { id: 'a1', name: 'site-a-arm1', group: 'g', arch: 'arm64', exit_code: 0, stdout: 'up 3 days\n', stderr: '', duration_ms: 40 },
    });
    emit({
      type: 'node_done',
      completed: 2,
      result: { id: 'b2', name: 'site-b-ec1', group: 'g', arch: 'amd64', exit_code: 255, stdout: '', stderr: 'ssh: connect timed out', duration_ms: 900 },
    });
    emit({ type: 'complete', success: 1, failed: 1, elapsed_sec: 1.5 });

    expect(await screen.findByText('[site-a-arm1] up 3 days')).toBeTruthy();
    expect(screen.getByText('[site-b-ec1] ssh: connect timed out')).toBeTruthy();
    expect(screen.getByText(ko.ops.run.store.complete(1, 1, '1.50'))).toBeTruthy();
    expect(screen.getByText(ko.ops.run.nodesProgress(2, 2))).toBeTruthy();

    const table = screen.getByRole('table', { name: ko.ops.run.results.title });
    expect(within(table).getByText(ko.ops.run.rowStatus.SUCCESS)).toBeTruthy();
    expect(within(table).getByText(ko.ops.run.rowStatus.FAILED)).toBeTruthy();
    expect(within(table).getByText('255')).toBeTruthy();
    expect(screen.getByText(ko.ops.run.finishedWithFailures)).toBeTruthy();
  });

  it('does not run when the operator cancels the confirmation', async () => {
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    pick('all');
    run('reboot');
    fireEvent.click(await screen.findByRole('button', { name: ko.shell.confirm.cancel }));
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('marks unfinished nodes and shows an error when the stream is lost', async () => {
    mount();
    await startFleetRun();
    emit({ type: 'node_start', node: servers[0] });
    fail(new Event('error'));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(ko.ops.run.store.streamLost);
    const table = screen.getByRole('table', { name: ko.ops.run.results.title });
    expect(within(table).getAllByText(ko.ops.run.rowStatus['NO RESULT'])).toHaveLength(2);
    expect(within(table).queryByText(ko.ops.run.rowStatus.SUCCESS)).toBeNull();
  });

  it('settles unfinished nodes as no result when the hub reports an error', async () => {
    mount();
    await startFleetRun();
    emit({ type: 'node_start', node: servers[0] });
    emit({ type: 'error', message: 'hub exploded' });
    expect((await screen.findByRole('alert')).textContent).toContain('hub exploded');
    const table = screen.getByRole('table', { name: ko.ops.run.results.title });
    expect(within(table).getAllByText(ko.ops.run.rowStatus['NO RESULT'])).toHaveLength(2);
  });

  it('uses the danger tone for the fleet-wide confirm and keeps the group confirm primary', async () => {
    mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    pick('all');
    run('uptime');
    expect((await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet })).className).toContain('danger');
    fireEvent.click(screen.getByRole('button', { name: ko.shell.confirm.cancel }));
    const option = (await screen.findByRole('option', { name: ko.ops.run.targetGroup('Site B', 1) })) as HTMLOptionElement;
    pick(option.value);
    run('uptime');
    expect(screen.getByRole('button', { name: ko.ops.run.confirm.confirmGroup }).className).not.toContain('danger');
  });

  it('runs on a node group after confirmation and sends only its resolved node ids', async () => {
    mount();
    const option = (await screen.findByRole('option', { name: ko.ops.run.targetGroup('Site B', 1) })) as HTMLOptionElement;
    pick(option.value);
    run('uptime');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog', { name: ko.ops.run.confirm.titleGroup('Site B') });
    expect(within(dialog).getByText(ko.ops.run.confirm.body(1, 'Site B'))).toBeTruthy();
    expect(within(dialog).getByLabelText(ko.ops.run.confirm.nodesAria).textContent).toBe('site-b-ec1');
    expect(within(dialog).getByText('uptime')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.ops.run.confirm.confirmGroup }));
    expect(mocked.streamFleetExec).toHaveBeenCalledWith('uptime', ['b2'], expect.any(Function), expect.any(Function), 15);
    expect(mocked.executeRemoteSSH).not.toHaveBeenCalled();
    const table = screen.getByRole('table', { name: ko.ops.run.results.title });
    expect(within(table).getByText('site-b-ec1')).toBeTruthy();
    expect(within(table).queryByText('site-a-arm1')).toBeNull();
    expect(screen.getByRole('button', { name: ko.ops.run.stopWatching })).toBeTruthy();
  });

  it('never runs a vanished group as a host name', async () => {
    mount('group:ghost');
    await screen.findByRole('option', { name: SITE_B_NODE });
    // The unknown group falls back to the local node, so nothing is streamed or sent as a host.
    await waitFor(() => expect(target()).toBe('local'));
    mocked.executeCommand.mockResolvedValue({ exit_code: 0, stdout: '', stderr: '', duration_ms: 1 });
    run('uptime');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    expect(mocked.executeRemoteSSH).not.toHaveBeenCalled();
  });

  it('does not start anything for a group target while the group list is unavailable', async () => {
    mocked.fetchGroups.mockRejectedValue(new Error('groups down'));
    mount('group:g1');
    expect(await screen.findByText('groups down')).toBeTruthy();
    await screen.findByRole('option', { name: SITE_B_NODE });
    run('uptime');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    expect(mocked.executeRemoteSSH).not.toHaveBeenCalled();
  });

  it('shows the node-list error with a retry', async () => {
    mocked.fetchServers.mockRejectedValueOnce(new Error('inventory down'));
    mount();
    expect(await screen.findByText('inventory down')).toBeTruthy();
    mocked.fetchServers.mockResolvedValue(servers);
    fireEvent.click(screen.getByRole('button', { name: ko.shell.dataGate.retry }));
    await screen.findByRole('option', { name: SITE_B_NODE });
    expect(screen.queryByText('inventory down')).toBeNull();
  });

  it('says when group targets are unavailable', async () => {
    mocked.fetchGroups.mockRejectedValue(new Error('groups down'));
    mount();
    expect(await screen.findByText('groups down')).toBeTruthy();
    await screen.findByRole('option', { name: SITE_B_NODE });
    expect(screen.queryByRole('option', { name: /^그룹:/ })).toBeNull();
  });
});

describe('Run: state survives navigation', () => {
  it('keeps the stream alive while the page is gone and shows later events on return', async () => {
    mount();
    await startFleetRun();
    emit({ type: 'start', command: 'uptime', total: 2 });
    emit({
      type: 'node_done',
      completed: 1,
      result: { id: 'a1', name: 'site-a-arm1', group: 'g', arch: 'arm64', exit_code: 0, stdout: 'first\n', stderr: '', duration_ms: 40 },
    });

    leave();
    expect(screen.getByText('elsewhere')).toBeTruthy();
    expect(cancel).not.toHaveBeenCalled();
    expect(screen.getByTestId('running-flag').textContent).toBe('yes');

    // Events keep landing while the Run page is not mounted.
    emit({
      type: 'node_done',
      completed: 2,
      result: { id: 'b2', name: 'site-b-ec1', group: 'g', arch: 'amd64', exit_code: 0, stdout: 'second\n', stderr: '', duration_ms: 50 },
    });

    leave();
    expect(mocked.streamFleetExec).toHaveBeenCalledTimes(1);
    expect(target()).toBe('all');
    expect(screen.getByText('[site-a-arm1] first')).toBeTruthy();
    expect(screen.getByText('[site-b-ec1] second')).toBeTruthy();
    expect(screen.getByText(ko.ops.run.nodesProgress(2, 2))).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.ops.run.stopWatching })).toBeTruthy();

    emit({ type: 'complete', success: 2, failed: 0, elapsed_sec: 1 });
    expect(await screen.findByText(ko.ops.run.finished)).toBeTruthy();
    expect(screen.getByTestId('running-flag').textContent).toBe('no');
    expect(cancel).not.toHaveBeenCalled();
  });

  it('Stop watching cancels the stream, settles rows and ignores late events', async () => {
    mount();
    await startFleetRun();
    emit({ type: 'node_start', node: servers[0] });
    fireEvent.click(screen.getByRole('button', { name: ko.ops.run.stopWatching }));

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(screen.getByText(ko.ops.run.stopped)).toBeTruthy();
    expect(screen.getByText(ko.ops.run.store.stopped)).toBeTruthy();
    const table = screen.getByRole('table', { name: ko.ops.run.results.title });
    expect(within(table).getAllByText(ko.ops.run.rowStatus['NO RESULT'])).toHaveLength(2);
    expect(screen.getByTestId('running-flag').textContent).toBe('no');

    // A late event from the cancelled stream must not resurrect the run.
    emit({
      type: 'node_done',
      completed: 1,
      result: { id: 'a1', name: 'site-a-arm1', group: 'g', arch: 'arm64', exit_code: 0, stdout: 'late\n', stderr: '', duration_ms: 1 },
    });
    expect(screen.queryByText('[site-a-arm1] late')).toBeNull();
    expect(screen.getByText(ko.ops.run.stopped)).toBeTruthy();
  });

  it('cancels the stream only when the whole app unmounts', async () => {
    const { unmount } = mount();
    await startFleetRun();
    leave();
    expect(cancel).not.toHaveBeenCalled();
    unmount();
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it('does not touch state after unmount (late stream events and single results)', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    let finish: (r: { exit_code: number; stdout: string; stderr: string; duration_ms: number }) => void = () => undefined;
    mocked.executeCommand.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { unmount } = mount();
    await screen.findByRole('option', { name: SITE_B_NODE });
    run('slow');
    unmount();
    await act(async () => finish({ exit_code: 0, stdout: 'x\n', stderr: '', duration_ms: 1 }));
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it('locks the target mid-run: a ?target= arriving then does not change it', async () => {
    const { rerender } = mount();
    await startFleetRun();
    expect(screen.getByLabelText(ko.ops.run.target)).toHaveProperty('disabled', true);
    // The route changes to #/run?target=b2 while the stream is still running.
    rerender(
      <GuardianRoot>
        <HubStoreProvider>
          <RunStoreProvider>
            <Harness urlTarget="b2" />
          </RunStoreProvider>
        </HubStoreProvider>
      </GuardianRoot>
    );
    expect(target()).toBe('all');
    expect(screen.getByLabelText(ko.ops.run.target)).toHaveProperty('disabled', true);
  });

  it('a new run replaces a finished one', async () => {
    mount();
    await startFleetRun('uptime');
    emit({ type: 'complete', success: 2, failed: 0, elapsed_sec: 1 });
    run('df -h');
    fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
    expect(mocked.streamFleetExec).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(`[${ko.ops.run.labelFleet}] uptime`)).toBeNull();
    expect(screen.getByText(`[${ko.ops.run.labelFleet}] df -h`)).toBeTruthy();
  });
});

describe('Run: ?target=', () => {
  beforeEach(() => {
    mocked.executeRemoteSSH.mockResolvedValue({ exit_code: 0, stdout: 'ok\n', stderr: '', duration_ms: 3 });
  });

  it('preselects a node, so a run goes to it without confirmation', async () => {
    mount('b2');
    await screen.findByRole('option', { name: SITE_B_NODE });
    await waitFor(() => expect(target()).toBe('b2'));
    run('uptime');
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(mocked.executeRemoteSSH).toHaveBeenCalledWith('b2', 'uptime'));
    expect(mocked.executeCommand).not.toHaveBeenCalled();
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
  });

  it('preselects a group, which needs confirmation and sends its members', async () => {
    mount('group:site-b');
    await screen.findByRole('option', { name: ko.ops.run.targetGroup('Site B', 1) });
    await waitFor(() => expect(target()).toBe('group:site-b'));
    run('uptime');
    expect(mocked.streamFleetExec).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmGroup }));
    expect(mocked.streamFleetExec).toHaveBeenCalledWith('uptime', ['b2'], expect.any(Function), expect.any(Function), 15);
  });

  it('preselects "all" and "local"', async () => {
    const first = mount('all');
    await screen.findByRole('option', { name: SITE_B_NODE });
    await waitFor(() => expect(target()).toBe('all'));
    first.unmount();
    mount('local');
    await screen.findByRole('option', { name: SITE_B_NODE });
    expect(target()).toBe('local');
  });

  it('falls back to the local node for an id the hub does not list', async () => {
    mount('ghost');
    await screen.findByRole('option', { name: SITE_B_NODE });
    await waitFor(() => expect(target()).toBe('local'));
  });
});

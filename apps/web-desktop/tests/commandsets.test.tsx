import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { en } from '../src/i18n/en';
import { ko } from '../src/i18n/ko';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { FleetPage } from '../src/pages/FleetPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import type { CommandSet, CommandSetList } from '../src/types';

const mocked = vi.mocked(api);
const C = ko.debate.commandSet;
const settle = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });

const builtin: CommandSet = {
  id: 'refresh-index', description: 'Refresh the package index', source: 'builtin', os: ['linux'], needs_sudo: true,
  params: {}, check: 'ls /var/lib/apt/lists', script: 'apt-get update -q', reversible: true, undo: 'nothing', risk: 'low',
  timeout: 600, reviewed: '2026-10-01',
};
const mine: CommandSet = { ...builtin, id: 'old-logs', description: 'Remove old logs', source: 'operator', reversible: false, risk: 'medium', reviewed: '2026-01-02' };
const list: CommandSetList = { ok: true, max_age_days: 180, stale: [{ id: 'old-logs', reviewed: '2026-01-02', age_days: 291 }], sets: [builtin, mine] };

function Routed() {
  const { params, query } = useRoute();
  return <FleetPage params={params} query={query} />;
}

async function mount() {
  window.location.hash = '#/fleet/commandSets';
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle();
}

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });

beforeEach(() => {
  for (const fn of [mocked.fetchCommandSets, mocked.saveCommandSet, mocked.deleteCommandSet, mocked.confirmCommandSet, mocked.fetchServers, mocked.fetchGroups]) fn.mockReset();
  mocked.fetchServers.mockResolvedValue([]);
  mocked.fetchGroups.mockResolvedValue({ groups: [], ungrouped: [] } as never);
  mocked.fetchCommandSets.mockResolvedValue(list);
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe('Command sets editor (Fleet commandSets tab)', () => {
  it('keeps the catalog the same shape in ko and en', () => {
    expect(Object.keys(en.debate.commandSet).sort()).toEqual(Object.keys(ko.debate.commandSet).sort());
  });

  it('lists builtin and own sets with risk, reversibility and a stale review', async () => {
    await mount();
    expect(screen.getByText(C.sectionCount(2))).toBeTruthy();
    expect(screen.getByText('refresh-index')).toBeTruthy();
    expect(screen.getByText(C.builtinChip)).toBeTruthy();
    expect(screen.getByText(C.operatorChip)).toBeTruthy();
    expect(screen.getByText(C.irreversible)).toBeTruthy();
    expect(screen.getByText(new RegExp(C.stale(291)))).toBeTruthy();
  });

  it('opens a builtin set read-only: it cannot be saved under its own id, only as a copy', async () => {
    mocked.saveCommandSet.mockResolvedValue({ id: 'refresh-copy', warnings: [] });
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.viewAria('refresh-index') }));
    const dialog = screen.getByRole('dialog', { name: C.viewTitle('refresh-index') });
    expect(within(dialog).queryByRole('button', { name: C.deleteAction })).toBeNull();
    const save = within(dialog).getByRole('button', { name: C.save }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    change(within(dialog).getByLabelText(C.id), 'refresh-copy');
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await settle();
    expect(mocked.saveCommandSet).toHaveBeenCalledTimes(1);
    expect(mocked.saveCommandSet.mock.calls[0][0]).toMatchObject({ id: 'refresh-copy', script: 'apt-get update -q', risk: 'low', reversible: true });
    expect(screen.getByText(C.saved('refresh-copy'))).toBeTruthy();
    expect(mocked.fetchCommandSets).toHaveBeenCalledTimes(2);
  });

  it('creates a set and keeps a hub refusal inside the dialog', async () => {
    mocked.saveCommandSet.mockRejectedValue(new api.HubError('script does not parse', 400));
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.newButton }));
    const dialog = screen.getByRole('dialog', { name: C.newTitle });
    change(within(dialog).getByLabelText(C.id), 'x-set');
    change(within(dialog).getByLabelText(C.description), 'd');
    change(within(dialog).getByLabelText(C.script), 'if then fi');
    fireEvent.click(within(dialog).getByRole('button', { name: C.save }));
    await settle();
    expect(within(dialog).getByText('script does not parse')).toBeTruthy();
    expect(mocked.saveCommandSet.mock.calls[0][0]).toMatchObject({ id: 'x-set', os: ['linux'], params: {}, timeout: 600 });
  });

  it('refuses an id that already exists instead of replacing that set', async () => {
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.newButton }));
    const dialog = screen.getByRole('dialog', { name: C.newTitle });
    change(within(dialog).getByLabelText(C.id), 'old-logs');
    fireEvent.click(within(dialog).getByRole('button', { name: C.save }));
    await settle();
    expect(within(dialog).getByText(C.idTaken('old-logs'))).toBeTruthy();
    expect(mocked.saveCommandSet).not.toHaveBeenCalled();
  });

  it('refuses parameters that are not a JSON object before calling the hub', async () => {
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.newButton }));
    const dialog = screen.getByRole('dialog', { name: C.newTitle });
    change(within(dialog).getByLabelText(C.params), '[1, 2');
    fireEvent.click(within(dialog).getByRole('button', { name: C.save }));
    await settle();
    expect(within(dialog).getByText(C.badParams)).toBeTruthy();
    expect(mocked.saveCommandSet).not.toHaveBeenCalled();
  });

  it('never sends a high risk set as undoable', async () => {
    mocked.saveCommandSet.mockResolvedValue({ id: 'old-logs', warnings: [] });
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.editAria('old-logs') }));
    const dialog = screen.getByRole('dialog', { name: C.editTitle('old-logs') });
    change(within(dialog).getByLabelText(C.risk), 'high');
    fireEvent.click(within(dialog).getByRole('button', { name: C.save }));
    await settle();
    expect(mocked.saveCommandSet.mock.calls[0][0]).toMatchObject({ risk: 'high', reversible: false });
  });

  it('deletes only own sets, after a confirmation', async () => {
    mocked.deleteCommandSet.mockResolvedValue(undefined);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: C.editAria('old-logs') }));
    const dialog = screen.getByRole('dialog', { name: C.editTitle('old-logs') });
    expect((within(dialog).getByLabelText(C.id) as HTMLInputElement).readOnly).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: C.deleteAction }));
    const confirm = screen.getByText(C.deleteConfirm('old-logs')).closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(confirm).getByRole('button', { name: C.deleteAction }));
    await settle();
    expect(mocked.deleteCommandSet).toHaveBeenCalledWith('old-logs');
    expect(screen.getByText(C.deleted('old-logs'))).toBeTruthy();
  });

  it('says whether an own set may run on its own and confirms it only after a second look', async () => {
    mocked.confirmCommandSet.mockResolvedValue(undefined);
    await mount();
    expect(screen.getAllByText(C.autoUnconfirmed)).toHaveLength(1); // only the operator's set carries the chip
    expect(screen.queryByText(C.autoConfirmed)).toBeNull();
    expect(screen.queryByRole('button', { name: C.confirmAria('refresh-index') })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: C.confirmAria('old-logs') }));
    expect(mocked.confirmCommandSet).not.toHaveBeenCalled();
    const confirm = screen.getByText(C.confirmMessage('old-logs')).closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(confirm).getByRole('button', { name: C.confirmButton }));
    await settle();
    expect(mocked.confirmCommandSet).toHaveBeenCalledWith('old-logs');
    expect(screen.getByText(C.confirmed('old-logs'))).toBeTruthy();
    expect(mocked.fetchCommandSets).toHaveBeenCalledTimes(2);
  });

  it('shows a confirmed set without a confirm button and keeps a failed confirmation inside its dialog', async () => {
    mocked.fetchCommandSets.mockResolvedValue({ ...list, sets: [builtin, { ...mine, confirmed: true }] });
    await mount();
    expect(screen.getByText(C.autoConfirmed)).toBeTruthy();
    expect(screen.queryByRole('button', { name: C.confirmAria('old-logs') })).toBeNull();

    mocked.fetchCommandSets.mockResolvedValue(list);
    mocked.confirmCommandSet.mockRejectedValue(new api.HubError('no command set old-logs', 404));
    await mount();
    fireEvent.click(screen.getAllByRole('button', { name: C.confirmAria('old-logs') })[0]);
    const confirm = screen.getByText(C.confirmMessage('old-logs')).closest('[role="dialog"]') as HTMLElement;
    fireEvent.click(within(confirm).getByRole('button', { name: C.confirmButton }));
    await settle();
    expect(within(confirm).getByText('no command set old-logs')).toBeTruthy();
  });
});

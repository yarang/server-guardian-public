import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, groupList, metrics, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { bucketBy, filterByGroup, groupHealthRows, rulesText, UNGROUPED, UNGROUPED_KEY } from '../src/lib/groups';
import { NodesPage } from '../src/pages/NodesPage';
import { FleetPage } from '../src/pages/FleetPage';
import { useRoute } from '../src/router';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
function Routed() {
  return <NodesPage query={useRoute().query} />;
}
const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <Routed />
      </HubStoreProvider>
    </GuardianRoot>
  );

/** Group management lives on the Fleet Groups tab (`#/fleet/groups`). */
const renderGroupSettings = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <FleetPage params={['groups']} query={{}} />
      </HubStoreProvider>
    </GuardianRoot>
  );

const extra = { ...servers[1], id: 'c3', name: 'lonely', hostname: '10.0.0.3', tier: 'Tier-2' };

describe('group helpers', () => {
  it('puts a node under every group it is in and the rest under Ungrouped', () => {
    const buckets = bucketBy([...servers, extra], (s) => s.id, 'groups', groupList.groups);
    expect(buckets.map((b) => [b.label, b.items.map((s) => s.id)])).toEqual([
      ['Site A', ['a1']],
      ['ARM', ['a1']],
      ['Site B', ['b2']],
      [UNGROUPED, ['c3']],
    ]);
  });

  it('buckets by a field, or not at all', () => {
    const tiered = bucketBy([...servers, extra], (s) => s.id, 'tier', [], (s, f) => (f === 'tier' ? s.tier : s.arch));
    // The bucket order follows localeCompare, which differs between ICU builds for Latin vs Hangul labels.
    const byLabel = (a: [string, number], b: [string, number]) => a[0].localeCompare(b[0]);
    expect(tiered.map((b): [string, number] => [b.label, b.items.length]).sort(byLabel)).toEqual(
      [['Tier-2', 1] as [string, number], [ko.fleet.groupsLib.noField('tier'), 2] as [string, number]].sort(byLabel),
    );
    expect(bucketBy(servers, (s) => s.id, 'none', groupList.groups)).toHaveLength(1);
  });

  it('filters by group and computes per-group health', () => {
    expect(filterByGroup([...servers, extra], 'arm', groupList.groups).map((s) => s.id)).toEqual(['a1']);
    expect(filterByGroup([...servers, extra], UNGROUPED_KEY, groupList.groups).map((s) => s.id)).toEqual(['c3']);
    expect(groupHealthRows(diagnosis.results, groupList.groups).find((r) => r.group === 'Site B')).toMatchObject({ online: 0, total: 1 });
    expect(rulesText(groupList.groups[1].rules)).toBe('arch = arm64');
  });
});

describe('Fleet node groups (Nodes inventory and Groups tab management)', () => {
  beforeEach(() => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.previewGroup.mockResolvedValue({ members: ['a1'], warnings: [] });
  });

  it('shows a node under each of its groups and lists its group names in the inventory', async () => {
    renderPage();
    const siteA = await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') });
    const arm = screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') });
    expect(within(siteA).getByText('site-a-arm1')).toBeTruthy();
    expect(within(arm).getByText('site-a-arm1')).toBeTruthy();
    expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('Empty') })).toBeNull();
    expect(within(siteA).getByText('Site A, ARM')).toBeTruthy();
  });

  it('switches grouping and filters by group', async () => {
    renderPage();
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') });
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupBy), { target: { value: 'arch' } });
    expect(screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('arm64') })).toBeTruthy();
    expect(screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('amd64') })).toBeTruthy();

    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupBy), { target: { value: 'groups' } });
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupFilter), { target: { value: 'site-b' } });
    expect(await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') })).toBeNull());
  });

  it('falls back to an ungrouped list with the error when groups cannot load', async () => {
    mocked.fetchGroups.mockRejectedValue(new Error('groups db locked'));
    renderPage();
    expect((await screen.findAllByText('groups db locked')).length).toBeGreaterThan(0);
    expect(await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria(ko.fleet.groupsLib.allNodes) })).toBeTruthy();
  });

  it('no longer manages groups on Nodes and links to the Fleet Groups tab', async () => {
    renderPage();
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') });
    expect(screen.queryByRole('table', { name: ko.fleet.groupsSection.tableLabel })).toBeNull();
    expect(screen.queryByRole('button', { name: ko.fleet.groupsSection.newGroup })).toBeNull();
    expect(screen.getByRole('link', { name: ko.fleet.nodes.manageGroups }).getAttribute('href')).toBe('#/fleet/groups');
  });

  it('lists groups with member counts and warnings', async () => {
    renderGroupSettings();
    const table = await screen.findByRole('table', { name: ko.fleet.groupsSection.tableLabel });
    expect(within(table).getByText('every ARM node')).toBeTruthy();
    expect(within(table).getByText(new RegExp(`${ko.fleet.groupSummary.rules('arch = arm64')}`))).toBeTruthy();
    expect(within(table).getByText('⚠ include: gone is not in servers.json')).toBeTruthy();
    expect(within(table).getAllByText(ko.fleet.groupsSection.nodesCount(1)).length).toBe(3);
  });

  it('creates a group from picked nodes and a rule, with a live preview', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'lab', members: ['a1', 'b2'], warnings: [] });
    renderGroupSettings();
    await screen.findByRole('table', { name: ko.fleet.groupsSection.tableLabel });
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.groupsSection.newGroup }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.name), { target: { value: ' Lab ' } });
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.groupDialog.includeAria('site-b-ec1')));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.groupDialog.addRule }));
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.fieldAria(ko.fleet.groupDialog.conditionLabel(1, 1))), { target: { value: 'name' } });
    // eq is valid for name too, so the operator is kept; switch it to glob.
    expect((within(dialog).getByLabelText(ko.fleet.groupDialog.operatorAria(ko.fleet.groupDialog.conditionLabel(1, 1))) as HTMLSelectElement).value).toBe('eq');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.operatorAria(ko.fleet.groupDialog.conditionLabel(1, 1))), { target: { value: 'glob' } });
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.valueAria(ko.fleet.groupDialog.conditionLabel(1, 1))), { target: { value: 'site-a-* ' } });

    await waitFor(() =>
      expect(mocked.previewGroup).toHaveBeenLastCalledWith({
        include: ['b2'],
        rules: [{ conditions: [{ field: 'name', op: 'glob', value: 'site-a-* ' }] }],
        exclude: [],
      })
    );
    expect(await within(dialog).findByText(ko.fleet.groupDialog.membersNow(1))).toBeTruthy();

    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.groupDialog.saveGroup }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith({
        name: 'Lab',
        description: undefined,
        include: ['b2'],
        rules: [{ conditions: [{ field: 'name', op: 'glob', value: 'site-a-*' }] }],
        exclude: [],
      })
    );
    expect(await screen.findByText(ko.fleet.groupsSection.saved('lab', 2))).toBeTruthy();
    expect(mocked.fetchGroups.mock.calls.length).toBeGreaterThan(1);
  });

  it('edits a group by id and shows the hub validation error', async () => {
    mocked.saveGroup.mockRejectedValue(new api.HubError("group name 'Site B' is already used by site-b", 400));
    renderGroupSettings();
    await screen.findByRole('table', { name: ko.fleet.groupsSection.tableLabel });
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.groupsSection.editAria('ARM') }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.name), { target: { value: 'Site B' } });
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.groupDialog.excludeAria('site-a-arm1')));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.groupDialog.saveGroup }));
    expect(await within(dialog).findByText("group name 'Site B' is already used by site-b")).toBeTruthy();
    expect(mocked.saveGroup).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'arm', name: 'Site B', exclude: ['a1'], rules: groupList.groups[1].rules })
    );
  });

  it('deletes a group only after confirmation', async () => {
    mocked.deleteGroup.mockResolvedValue(undefined);
    renderGroupSettings();
    await screen.findByRole('table', { name: ko.fleet.groupsSection.tableLabel });
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.groupsSection.editAria('Empty') }));
    fireEvent.click(await screen.findByRole('button', { name: ko.fleet.groupDialog.deleteGroup }));
    expect(mocked.deleteGroup).not.toHaveBeenCalled();
    const confirm = screen.getAllByRole('dialog').at(-1) as HTMLElement;
    fireEvent.click(within(confirm).getByRole('button', { name: ko.fleet.groupDialog.deleteGroup }));
    await waitFor(() => expect(mocked.deleteGroup).toHaveBeenCalledWith('empty'));
    expect(await screen.findByText(ko.fleet.groupsSection.deleted('empty'))).toBeTruthy();
  });
});

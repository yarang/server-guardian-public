import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, fleetSSH, groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { buildGroupDebateTask } from '../src/lib/debateTask';
import { groupData, membershipOrigin, planAdd, planRemove } from '../src/lib/groupEdit';
import { groupReachability } from '../src/lib/groups';
import { NodeDetailPage } from '../src/pages/NodeDetailPage';
import { NodesPage } from '../src/pages/NodesPage';
import { FleetPage } from '../src/pages/FleetPage';
import { useRoute } from '../src/router';
import { getDebatePrefill } from '../src/state/debatePrefill';
import { HubStoreProvider } from '../src/state/hubStore';
import type { NodeGroupList } from '../src/types';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);

/** Adds a group that excludes a1 and one that matches b2 by rule only, next to the shared fixture groups. */
const lab = { id: 'lab', name: 'Lab', description: 'scratch', tone: 'info' as const, include: ['b2'], rules: [], exclude: ['a1'], members: ['b2'], warnings: [] };
const amd = {
  id: 'amd',
  name: 'AMD',
  include: [],
  rules: [{ conditions: [{ field: 'arch' as const, op: 'eq' as const, value: 'amd64' }] }],
  exclude: [],
  members: ['b2'],
  warnings: [],
};
const groups: NodeGroupList = { ...groupList, groups: [...groupList.groups, lab, amd] };
const [siteA, arm, siteB] = groupList.groups;

function Routed() {
  return <NodesPage query={useRoute().query} />;
}
const wrap = (ui: React.ReactNode) =>
  render(
    <GuardianRoot>
      <HubStoreProvider>{ui}</HubStoreProvider>
    </GuardianRoot>
  );

beforeEach(() => {
  window.location.hash = '';
  mocked.fetchMetrics.mockResolvedValue({} as never);
  mocked.fetchServers.mockResolvedValue(servers);
  mocked.diagnoseFleet.mockResolvedValue(diagnosis);
  mocked.fetchGroups.mockResolvedValue(groups);
  mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
  mocked.previewGroup.mockResolvedValue({ members: ['a1', 'b2'], warnings: [] });
  mocked.saveGroup.mockReset();
});

describe('group edit helpers', () => {
  it('derives membership origin from the definition and members', () => {
    expect(membershipOrigin(siteA, 'a1')).toBe('added');
    expect(membershipOrigin(arm, 'a1')).toBe('rule');
    expect(membershipOrigin(arm, 'b2')).toBeNull();
  });

  it('strips resolved fields from the saved definition', () => {
    expect(groupData(arm)).toEqual({ id: 'arm', name: 'ARM', description: 'every ARM node', include: [], rules: arm.rules, exclude: [] });
    expect(Object.keys(groupData(lab)).sort()).toEqual(['description', 'exclude', 'id', 'include', 'name', 'rules', 'tone']);
  });

  it('adds ids deduplicated and lifts exclusions', () => {
    const plan = planAdd(lab, ['a1', 'b2', 'a1']);
    expect(plan.added).toEqual(['a1']);
    expect(plan.unchanged).toEqual(['b2']);
    expect(plan.unexcluded).toEqual(['a1']);
    expect(plan.data.include).toEqual(['b2', 'a1']);
    expect(plan.data.exclude).toEqual([]);
    expect(planAdd(siteA, ['a1']).changes).toBe(false);
  });

  it('removes an explicit member from include and excludes a rule match', () => {
    expect(planRemove(siteA, 'a1')).toMatchObject({ how: 'include', data: { include: [], exclude: [] } });
    expect(planRemove(arm, 'a1')).toMatchObject({ how: 'exclude', data: { include: [], exclude: ['a1'], rules: arm.rules } });
    expect(planRemove(arm, 'b2')).toBeNull();
  });

  it('computes group reachability only from the diagnosis', () => {
    const two = { ...siteB, members: ['a1', 'b2', 'zz'] };
    expect(groupReachability(two, servers, diagnosis)).toEqual({ probed: true, total: 3, online: 1, offline: ['site-b-ec1'], notProbed: 1 });
    expect(groupReachability(two, servers, null)).toMatchObject({ probed: false, online: 0, offline: [], notProbed: 3 });
  });

  it('builds a group-scoped debate task from real data and states gaps', () => {
    const task = buildGroupDebateTask({ ...lab, members: ['a1', 'b2'] }, servers, diagnosis, fleetSSH);
    expect(task).toContain('node group "Lab" (lab)');
    expect(task).toContain('Scope: the 2 members of this group only.');
    expect(task).toContain('Members: site-a-arm1, site-b-ec1.');
    expect(task).toContain('Reachability: 1/2 members online.');
    expect(task).toContain('Offline: site-b-ec1');
    expect(task).toContain('SSH audit (scope:');
    const bare = buildGroupDebateTask(arm, servers, null, null);
    expect(bare).toContain('Reachability: not available (no fleet diagnosis).');
    expect(bare).toContain('SSH audit: not available.');
    expect(buildGroupDebateTask(arm, servers, null)).not.toContain('SSH audit');
  });
});

describe('Nodes multi-select', () => {
  const open = async () => {
    wrap(<Routed />);
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') });
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupBy), { target: { value: 'none' } });
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria(ko.fleet.groupsLib.allNodes) });
  };

  it('selects rows, selects all visible and clears', async () => {
    await open();
    expect(screen.queryByRole('region', { name: ko.fleet.nodes.selectionAria })).toBeNull();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAria('site-a-arm1')));
    expect(within(screen.getByRole('region', { name: ko.fleet.nodes.selectionAria })).getByText(ko.fleet.nodes.selected(1))).toBeTruthy();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAllAria));
    expect(screen.getByText(ko.fleet.nodes.selected(2))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.clearSelection }));
    expect(screen.queryByText(ko.fleet.nodes.selected(2))).toBeNull();
  });

  it('drops selected servers a filter hides', async () => {
    await open();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAllAria));
    expect(screen.getByText(ko.fleet.nodes.selected(2))).toBeTruthy();
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupFilter), { target: { value: 'site-b' } });
    expect(await screen.findByText(ko.fleet.nodes.selected(1))).toBeTruthy();
    expect((screen.getByLabelText(ko.fleet.nodes.selectAria('site-b-ec1')) as HTMLInputElement).checked).toBe(true);
  });

  it('creates a group from the selection with the picked ids in include and an empty name', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'pair', members: ['a1', 'b2'], warnings: [] });
    await open();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAllAria));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.createFromSelection }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByLabelText(ko.fleet.groupDialog.name) as HTMLInputElement).value).toBe('');
    expect((within(dialog).getByRole('button', { name: ko.fleet.groupDialog.saveGroup }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByLabelText(ko.fleet.groupDialog.includeAria('site-a-arm1')) as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText(ko.fleet.groupDialog.includeAria('site-b-ec1')) as HTMLInputElement).checked).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.groupDialog.name), { target: { value: 'Pair' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.groupDialog.saveGroup }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith({ name: 'Pair', description: undefined, include: ['a1', 'b2'], rules: [], exclude: [] })
    );
    expect(await screen.findByText(ko.fleet.nodes.savedGroup('pair', 2))).toBeTruthy();
    expect(mocked.fetchGroups.mock.calls.length).toBeGreaterThan(1);
    expect(screen.queryByRole('region', { name: ko.fleet.nodes.selectionAria })).toBeNull();
  });

  it('adds the selection to a group: confirmation first, include deduped, exclude lifted', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'lab', members: ['a1', 'b2'], warnings: [] });
    await open();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAllAria));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.addToGroup }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByRole('button', { name: ko.fleet.addToGroup.add }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.addToGroup.group), { target: { value: 'lab' } });
    const what = within(dialog).getByRole('region', { name: ko.fleet.addToGroup.whatChanges });
    expect(what.textContent).toContain(ko.fleet.addToGroup.added('Lab', 'site-a-arm1'));
    expect(what.textContent).toContain(ko.fleet.addToGroup.unchanged('site-b-ec1'));
    expect(what.textContent).toContain(ko.fleet.addToGroup.unexcluded('Lab', 'site-a-arm1'));
    expect(mocked.saveGroup).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.addToGroup.add }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith({
        id: 'lab',
        name: 'Lab',
        description: 'scratch',
        tone: 'info',
        include: ['b2', 'a1'],
        rules: [],
        exclude: [],
      })
    );
    expect(await screen.findByText(ko.fleet.nodes.updatedGroup('Lab', 2))).toBeTruthy();
    expect(mocked.fetchGroups.mock.calls.length).toBeGreaterThan(1);
  });

  it('shows the hub validation error and keeps the dialog open', async () => {
    mocked.saveGroup.mockRejectedValue(new api.HubError('include: too many servers (max 500)', 400));
    await open();
    fireEvent.click(screen.getByLabelText(ko.fleet.nodes.selectAria('site-a-arm1')));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.addToGroup }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.addToGroup.group), { target: { value: 'site-b' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.addToGroup.add }));
    expect(await within(dialog).findByText('include: too many servers (max 500)')).toBeTruthy();
  });
});

describe('Node detail membership', () => {
  const detail = async (id: string) => {
    wrap(<NodeDetailPage id={id} />);
    return screen.findByRole('table', { name: ko.fleet.membership.tableLabel });
  };

  it('shows how each membership comes about', async () => {
    const table = await detail('a1');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Site A'),
      expect.stringContaining('ARM'),
    ]);
    expect(within(rows[0]).getByText(ko.fleet.membership.howAdded)).toBeTruthy();
    expect(within(rows[1]).getByText(ko.fleet.membership.howRule)).toBeTruthy();
    expect(within(table).getByRole('link', { name: 'ARM' }).getAttribute('href')).toBe('#/fleet/nodes?group=arm');
  });

  it('removes an explicit member from include after confirming', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'site-a', members: [], warnings: [] });
    const table = await detail('a1');
    fireEvent.click(within(table).getByRole('button', { name: ko.fleet.membership.removeAria('Site A') }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain(ko.fleet.membership.confirmInclude('site-a-arm1', 'Site A'));
    expect(mocked.saveGroup).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.membership.confirmLabel }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith({ id: 'site-a', name: 'Site A', description: undefined, tone: undefined, include: [], rules: [], exclude: [] })
    );
    expect(await screen.findByText(ko.fleet.membership.removed('Site A', 'site-a-arm1'))).toBeTruthy();
    expect(mocked.fetchGroups.mock.calls.length).toBeGreaterThan(1);
  });

  it('excludes a rule-matched member and says so', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'arm', members: [], warnings: [] });
    const table = await detail('a1');
    fireEvent.click(within(table).getByRole('button', { name: ko.fleet.membership.removeAria('ARM') }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain(ko.fleet.membership.confirmRule('site-a-arm1', 'ARM'));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.membership.confirmLabel }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith({
        id: 'arm',
        name: 'ARM',
        description: 'every ARM node',
        tone: undefined,
        include: [],
        rules: arm.rules,
        exclude: ['a1'],
      })
    );
  });

  it('flags a removal that leaves the node a member and shows save errors in the confirm', async () => {
    mocked.saveGroup.mockRejectedValueOnce(new api.HubError('nope', 400)).mockResolvedValueOnce({ id: 'site-b', members: ['b2'], warnings: [] });
    const table = await detail('b2');
    fireEvent.click(within(table).getByRole('button', { name: ko.fleet.membership.removeAria('Site B') }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.membership.confirmLabel }));
    expect(await within(dialog).findByText('nope')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.membership.confirmLabel }));
    expect(await screen.findByText(ko.fleet.membership.removedStill('Site B', 'site-b-ec1'))).toBeTruthy();
  });

  it('adds the node to a group, lifting its exclusion', async () => {
    mocked.saveGroup.mockResolvedValue({ id: 'lab', members: ['a1', 'b2'], warnings: [] });
    await detail('a1');
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.addToGroup }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.addToGroup.group), { target: { value: 'lab' } });
    expect(dialog.textContent).toContain(ko.fleet.addToGroup.unexcluded('Lab', 'site-a-arm1'));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.addToGroup.add }));
    await waitFor(() =>
      expect(mocked.saveGroup).toHaveBeenCalledWith(expect.objectContaining({ id: 'lab', include: ['b2', 'a1'], exclude: [] }))
    );
    expect(await screen.findByText(ko.fleet.membership.added('Lab', 'site-a-arm1'))).toBeTruthy();
  });
});

describe('Group summary on Nodes', () => {
  it('shows values from the group, the shared diagnosis and the member SSH audit, with its actions', async () => {
    wrap(<NodesPage query={{ group: 'lab' }} />);
    const panel = await screen.findByRole('region', { name: ko.fleet.groupSummary.title('Lab') });
    expect(panel.textContent).toContain('scratch');
    expect(panel.textContent).toContain(ko.fleet.groupSummary.members(1));
    expect(await within(panel).findByText(new RegExp(ko.fleet.groupSummary.online(0, 1).replace('/', '\\/')))).toBeTruthy();
    expect(panel.textContent).toContain(ko.fleet.groupSummary.offline('site-b-ec1'));
    expect(panel.textContent).toContain(ko.fleet.groupSummary.probedPrefix);
    await waitFor(() => expect(mocked.fetchFleetSSHAudit).toHaveBeenCalledWith(['b2'], false));
    expect(within(panel).getByRole('link', { name: ko.fleet.groupSummary.runOnGroup }).getAttribute('href')).toBe('#/fleet/commands?target=group%3Alab');
    expect(within(panel).getByRole('link', { name: ko.fleet.groupSummary.editGroup }).getAttribute('href')).toBe('#/fleet/groups?edit=lab');
  });

  it('prefills a group-scoped debate task', async () => {
    wrap(<NodesPage query={{ group: 'lab' }} />);
    const panel = await screen.findByRole('region', { name: ko.fleet.groupSummary.title('Lab') });
    await within(panel).findByText(new RegExp(ko.fleet.groupSummary.online(0, 1).replace('/', '\\/')));
    await waitFor(() => expect(mocked.fetchFleetSSHAudit).toHaveBeenCalled());
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.groupSummary.debateGroup }));
    expect(window.location.hash).toBe('#/debates');
    const prefill = getDebatePrefill();
    expect(prefill?.source).toBe('group');
    expect(prefill?.task).toContain('node group "Lab" (lab)');
    expect(prefill?.task).toContain('Reachability: 0/1 members online.');
  });

  it('says not probed without a diagnosis and shows no summary for an unknown group', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('probe failed'));
    const { unmount } = wrap(<NodesPage query={{ group: 'lab' }} />);
    const panel = await screen.findByRole('region', { name: ko.fleet.groupSummary.title('Lab') });
    expect(await within(panel).findByText(ko.fleet.groupSummary.notProbedRun)).toBeTruthy();
    expect(panel.textContent).not.toContain(ko.fleet.groupSummary.online(0, 1).slice(0, 4));
    unmount();
    wrap(<NodesPage query={{ group: 'nope' }} />);
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria(ko.fleet.groupsLib.allNodes) }).catch(() => undefined);
    expect(screen.queryByRole('region', { name: new RegExp(`^${ko.fleet.groupSummary.title('')}`) })).toBeNull();
  });

  it('shows no summary without a group filter', async () => {
    wrap(<NodesPage query={{}} />);
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') });
    expect(screen.queryByRole('region', { name: new RegExp(`^${ko.fleet.groupSummary.title('')}`) })).toBeNull();
  });
});

describe('Groups tab links and edit deep link', () => {
  const settings = (edit?: string) => wrap(<FleetPage params={['groups']} query={edit ? { edit } : {}} />);

  it('links group names to the filtered Nodes view', async () => {
    settings();
    const table = await screen.findByRole('table', { name: ko.fleet.groupsSection.tableLabel });
    expect(within(table).getByRole('link', { name: 'ARM' }).getAttribute('href')).toBe('#/fleet/nodes?group=arm');
  });

  it('opens the dialog for ?edit=<id> and clears the query on close', async () => {
    window.location.hash = '#/fleet/groups?edit=arm';
    settings('arm');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(ko.fleet.groupDialog.titleEdit('ARM'))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.cancel }));
    expect(window.location.hash).toBe('#/fleet/groups');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('warns about an unknown edit id instead of opening a dialog', async () => {
    settings('ghost');
    expect(await screen.findByText(ko.fleet.groupsSection.noGroupWithId('ghost'))).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

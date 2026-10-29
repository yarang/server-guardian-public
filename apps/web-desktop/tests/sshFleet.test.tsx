import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fleetSSH, groupList, keys, security, servers, sshConfig } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { averageScore, groupAuditRows, heatmapCell, heatmapColumns, heatmapRows, ruleColumns, worstRule } from '../src/lib/sshAudit';
import { UNGROUPED } from '../src/lib/groups';
import { SshPage } from '../src/pages/SshPage';
import { clearDebatePrefill, getDebatePrefill } from '../src/state/debatePrefill';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <SshPage />
      </HubStoreProvider>
    </GuardianRoot>
  );
const groupTable = () => screen.getByRole('table', { name: ko.fleet.ssh.byGroupLabel });

describe('fleet SSH audit helpers', () => {
  const columns = ruleColumns(fleetSSH);

  it('maps statuses to heatmap cells without inventing passes', () => {
    expect([heatmapCell('PASS'), heatmapCell('FAIL'), heatmapCell('WARN'), heatmapCell('INFO'), heatmapCell(undefined)]).toEqual(['P', 'F', 'W', 'I', 'N']);
    expect(columns.map((c) => c.label)).toEqual([ko.fleet.ssh.shortLabels['SEC-SSH-001'], ko.fleet.ssh.shortLabels['SEC-SSH-002'], ko.fleet.ssh.shortLabels['SEC-SSH-005']]);
    expect(heatmapColumns(columns)[0]).toEqual({ key: 'SEC-SSH-001', label: ko.fleet.ssh.shortLabels['SEC-SSH-001'], title: 'SEC-SSH-001: Disable Direct Root Login via SSH' });
    expect(heatmapRows(fleetSSH.nodes, columns)).toEqual([
      { id: 'a1', name: 'site-a-arm1', cells: ['W', 'P', 'I'], score: 85 },
      { id: 'b2', name: 'site-b-ec1', cells: ['F', 'F', 'P'], score: 40, status: { tone: 'warn', label: ko.fleet.ssh.nodeStatus.partial } },
      { id: 'c3', name: 'lonely', cells: [], unavailable: `${ko.fleet.ssh.nodeStatus.unreachable}: ssh: connect to host lonely port 22: No route to host` },
    ]);
    // A rule absent from a report is "not checked"; a node without an error still explains itself.
    const bare = { ...fleetSSH.nodes[0], report: { ...fleetSSH.nodes[0].report!, rules: [] } };
    expect(heatmapRows([bare, { ...fleetSSH.nodes[2], status: 'error' as const, error: undefined }], columns).map((r) => r.unavailable ?? r.cells)).toEqual([
      ['N', 'N', 'N'],
      `${ko.fleet.ssh.nodeStatus.error}: ${ko.fleet.ssh.notAudited}`,
    ]);
  });

  it('aggregates per group, counting a node in each of its groups', () => {
    const rows = groupAuditRows(fleetSSH, groupList.groups, columns);
    expect(rows.map((r) => [r.group, r.nodes, r.audited, r.avgScore, r.failing.map((n) => n.id), r.worst?.column.label ?? null])).toEqual([
      ['Site A', 1, 1, 85, [], null],
      ['ARM', 1, 1, 85, [], null],
      ['Site B', 1, 1, 40, ['b2'], ko.fleet.ssh.shortLabels['SEC-SSH-001']],
      [UNGROUPED, 1, 0, null, [], null],
    ]);
    expect(groupAuditRows(fleetSSH, null, columns).map((r) => [r.group, r.nodes])).toEqual([[ko.fleet.groupsLib.allNodes, 3]]);
    expect(averageScore(fleetSSH.nodes)).toBe(62.5);
    expect(averageScore([fleetSSH.nodes[2]])).toBeNull();
    expect(worstRule(fleetSSH.nodes, columns)).toMatchObject({ fail: 1, column: { id: 'SEC-SSH-001' } });
  });
});

describe('SshPage fleet audit', () => {
  beforeEach(() => {
    mocked.fetchFleetSSHAudit.mockReset();
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.fetchSSHSecurity.mockResolvedValue(security);
    mocked.fetchSSHConfig.mockResolvedValue(sshConfig);
    mocked.fetchAuthorizedKeys.mockResolvedValue(keys);
  });

  it('summarizes per group and shows a rules-by-node heatmap per group', async () => {
    renderPage();
    expect(await screen.findByText('62.5')).toBeTruthy();
    expect(mocked.fetchFleetSSHAudit).toHaveBeenCalledWith([], false);
    expect(screen.getByText(ko.fleet.ssh.reachabilityFallback(1, 0))).toBeTruthy();

    const table = await waitFor(groupTable);
    const siteB = within(table).getByText('Site B').closest('tr') as HTMLElement;
    // Failing node names link to their detail page.
    const failingLink = within(siteB).getByRole('link', { name: 'site-b-ec1' });
    expect(failingLink.getAttribute('href')).toBe('#/fleet/nodes/b2');
    expect(failingLink.closest('td')?.textContent).toBe('1: site-b-ec1');
    expect(within(siteB).getByText(ko.fleet.ssh.worst(ko.fleet.ssh.shortLabels['SEC-SSH-001'], 1))).toBeTruthy();
    const ungrouped = within(table).getByText(UNGROUPED).closest('tr') as HTMLElement;
    expect(within(ungrouped).getByText(ko.fleet.ssh.auditedWithUnreachable(0, 1))).toBeTruthy();
    expect(within(ungrouped).getAllByText('—')).toHaveLength(2); // no score, no worst rule

    const heatmap = screen.getByRole('region', { name: ko.fleet.ssh.rulesByNode });
    // a1 is in two groups, so its row appears under both; the unreachable node is an unavailable row.
    expect(within(heatmap).getAllByRole('button', { name: 'site-a-arm1' })).toHaveLength(2);
    const siteBRow = within(heatmap).getByRole('button', { name: 'site-b-ec1' }).closest('tr') as HTMLElement;
    expect(within(siteBRow).getByText(ko.fleet.ssh.nodeStatus.partial)).toBeTruthy();
    expect(within(heatmap).getAllByRole('img', { name: `${ko.fleet.ssh.shortLabels['SEC-SSH-001']}: ${ko.shell.ui.heatmap.words.F}` })).toHaveLength(1);
    expect(within(heatmap).getAllByRole('img', { name: `${ko.fleet.ssh.shortLabels['SEC-SSH-005']}: ${ko.shell.ui.heatmap.words.I}` })).toHaveLength(2);
    const reason = within(heatmap).getByText(`${ko.fleet.ssh.nodeStatus.unreachable}: ssh: connect to host lonely port 22: No route to host`);
    expect(reason.getAttribute('colspan')).toBe('3');
    expect(within(reason.closest('tr') as HTMLElement).getByRole('button', { name: 'lonely' })).toBeTruthy();
    expect(within(heatmap).getAllByRole('columnheader', { name: ko.fleet.ssh.shortLabels['SEC-SSH-001'] })[0].getAttribute('title')).toBe('SEC-SSH-001: Disable Direct Root Login via SSH');
    // Each group's legend lists only the states it shows.
    const legends = within(heatmap).getAllByRole('list', { name: ko.shell.ui.heatmap.legend });
    expect(legends.map((l) => within(l).getAllByRole('listitem').map((li) => li.textContent))).toEqual([
      ['✓' + ko.shell.ui.heatmap.words.P, '!' + ko.shell.ui.heatmap.words.W, 'i' + ko.shell.ui.heatmap.words.I],
      ['✓' + ko.shell.ui.heatmap.words.P, '!' + ko.shell.ui.heatmap.words.W, 'i' + ko.shell.ui.heatmap.words.I],
      ['✓' + ko.shell.ui.heatmap.words.P, '✕' + ko.shell.ui.heatmap.words.F],
    ]);
    expect(within(heatmap).getByText(ko.fleet.ssh.partialNote)).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'Nodes that could not be audited' })).toBeNull();
    expect(screen.queryByLabelText('Node details')).toBeNull();
  });

  it('opens on the first failing node and switches node details', async () => {
    renderPage();
    const details = await screen.findByRole('region', { name: ko.fleet.ssh.nodeSection('site-b-ec1') });
    expect(within(details).getByText(`${ko.fleet.ssh.remediation}: Set 'PasswordAuthentication no'`)).toBeTruthy();
    expect(within(details).getAllByText(`${ko.fleet.ssh.current}: yes · ${ko.fleet.ssh.expected}: no`, { exact: false })).toHaveLength(2);
    expect(within(details).getByText(/sshd -T could not run/)).toBeTruthy();
    expect(within(details).getByText(ko.fleet.ssh.nodeKeys(0))).toBeTruthy();
    expect(within(details).getByRole('link', { name: ko.fleet.ssh.openNode('site-b-ec1') }).getAttribute('href')).toBe('#/fleet/nodes/b2');

    const heatmap = screen.getByRole('region', { name: ko.fleet.ssh.rulesByNode });
    const siteBButton = within(heatmap).getByRole('button', { name: 'site-b-ec1' });
    expect(siteBButton.getAttribute('aria-pressed')).toBe('true');
    expect(siteBButton.closest('tr')!.className).toContain('sg-heatmap__row--selected');

    fireEvent.click(within(heatmap).getAllByRole('button', { name: 'site-a-arm1' })[1]);
    const a1 = screen.getByRole('region', { name: ko.fleet.ssh.nodeSection('site-a-arm1') });
    // a1 is highlighted in both of its groups' heatmaps.
    expect(within(heatmap).getAllByRole('button', { name: 'site-a-arm1' }).map((b) => b.getAttribute('aria-pressed'))).toEqual(['true', 'true']);
    expect(siteBButton.getAttribute('aria-pressed')).toBe('false');
    expect(within(a1).getByText('guardian-hub-fleet@hub')).toBeTruthy();
    expect(within(a1).getByText(ko.fleet.ssh.ruleStatus.INFO)).toBeTruthy();
    expect(within(a1).queryByText(ko.fleet.ssh.ruleStatus.PASS)).toBeNull(); // passing rules are not findings

    fireEvent.click(within(heatmap).getByRole('button', { name: 'lonely' }));
    const c3 = screen.getByRole('region', { name: ko.fleet.ssh.nodeSection('lonely') });
    expect(within(c3).getByText(ko.fleet.ssh.noFindings)).toBeTruthy();
  });

  it('hands the selected node and its findings to Debate', async () => {
    mocked.fetchServers.mockResolvedValue(servers);
    clearDebatePrefill();
    renderPage();
    const details = await screen.findByRole('region', { name: ko.fleet.ssh.nodeSection('site-b-ec1') });
    fireEvent.click(await within(details).findByRole('button', { name: ko.fleet.ssh.debateNode }));
    const prefill = getDebatePrefill();
    expect(prefill?.task).toContain('site-b-ec1 (b2)');
    expect(prefill?.task).toContain('FAIL Disable Direct Root Login via SSH');
    expect(window.location.hash).toBe('#/debates');
    clearDebatePrefill();
  });

  it('offers no debate for a node the inventory does not know', async () => {
    mocked.fetchServers.mockResolvedValue([]);
    renderPage();
    const details = await screen.findByRole('region', { name: ko.fleet.ssh.nodeSection('site-b-ec1') });
    expect(within(details).queryByRole('button', { name: ko.fleet.ssh.debateNode })).toBeNull();
  });

  it('filters the heatmap and node list by group', async () => {
    renderPage();
    await waitFor(groupTable);
    fireEvent.change(screen.getByLabelText(ko.fleet.ssh.groupFilter), { target: { value: 'site-b' } });
    const heatmap = screen.getByRole('region', { name: ko.fleet.ssh.rulesByNode });
    expect(within(heatmap).queryByText('site-a-arm1')).toBeNull();
    expect(within(heatmap).getByRole('button', { name: 'site-b-ec1' })).toBeTruthy();
    expect(within(heatmap).queryByText('lonely')).toBeNull();
    expect(within(heatmap).getAllByRole('table')).toHaveLength(1);
    // The overview table still covers every group.
    expect(within(groupTable()).getByText('Site A')).toBeTruthy();
  });

  it('asks the hub to probe again on Refresh audit', async () => {
    renderPage();
    await screen.findByText('62.5');
    mocked.fetchFleetSSHAudit.mockResolvedValueOnce({ ...fleetSSH, cached: true });
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.ssh.refresh }));
    expect(mocked.fetchFleetSSHAudit).toHaveBeenLastCalledWith([], true);
    expect(await screen.findByText(new RegExp(ko.fleet.ssh.auditedLine('', true, '').split(' · ')[1].replace(/[()]/g, '\\$&')))).toBeTruthy();
  });

  it('shows the failure instead of data and retries with a fresh probe', async () => {
    mocked.fetchFleetSSHAudit.mockRejectedValueOnce(new api.HubError('servers.json not found', 500));
    renderPage();
    const alert = await screen.findByText(ko.fleet.ssh.fleetFailed);
    expect(alert).toBeTruthy();
    expect(screen.getByText('servers.json not found')).toBeTruthy();
    expect(screen.queryByText('62.5')).toBeNull();
    fireEvent.click(within(alert.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: ko.common.retry }));
    expect(await screen.findByText('62.5')).toBeTruthy();
    expect(mocked.fetchFleetSSHAudit).toHaveBeenLastCalledWith([], true);
  });

  it('falls back to one fleet row when node groups fail to load', async () => {
    mocked.fetchGroups.mockRejectedValue(new Error('groups down'));
    renderPage();
    expect(await screen.findByText(ko.fleet.ssh.groupsFailed)).toBeTruthy();
    const table = groupTable();
    expect(within(table).getByText(ko.fleet.groupsLib.allNodes)).toBeTruthy();
    expect(within(table).queryByText(UNGROUPED)).toBeNull();
    expect((screen.getByLabelText(ko.fleet.ssh.groupFilter) as HTMLSelectElement).disabled).toBe(true);
  });

  it('says so when the hub has no nodes', async () => {
    mocked.fetchFleetSSHAudit.mockResolvedValue({
      ...fleetSSH,
      nodes: [],
      summary: { ...fleetSSH.summary, nodes: 0, audited: 0, unreachable: 0, avg_score: null, failing_nodes: [] },
    });
    renderPage();
    expect(await screen.findByText(ko.fleet.ssh.noNodesToAudit)).toBeTruthy();
  });
});

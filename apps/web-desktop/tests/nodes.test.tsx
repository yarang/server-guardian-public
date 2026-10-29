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
import { NodesPage } from '../src/pages/NodesPage';
import { useRoute } from '../src/router';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
/** Mirrors App: the page gets its query from the URL hash. */
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

describe('NodesPage', () => {
  beforeEach(() => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchGroups.mockResolvedValue(groupList);
  });

  it('shows a loading state, then real metrics, inventory and processes', async () => {
    renderPage();
    expect(screen.getAllByRole('status').length).toBeGreaterThan(0);
    expect(await screen.findByText('34.1')).toBeTruthy();
    expect(screen.getByText('4.0 / 8.0 GB')).toBeTruthy();
    expect(screen.getAllByText('site-a-arm1').length).toBeGreaterThan(0);
    expect(screen.getAllByText('site-b-ec1').length).toBeGreaterThan(0);
    // Arch, Mesh IP and Probe are behind the Columns toggle.
    expect(screen.queryByText('100.64.0.1')).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: ko.fleet.nodes.detailColumns }));
    // a1 is in two groups, so it is listed twice.
    expect(screen.getAllByText('100.64.0.1')).toHaveLength(2);
    expect(screen.getByText('/usr/local/bin/guardian-agent --sse')).toBeTruthy();
    expect(screen.getByRole('meter', { name: ko.fleet.hub.usageAria('CPU') })).toBeTruthy();
  });

  it('hides Arch, Mesh IP and Probe by default and remembers the Columns choice', async () => {
    const { unmount } = renderPage();
    await screen.findByText('34.1');
    const headers = () => screen.getAllByRole('columnheader').map((h) => h.textContent);
    expect(headers()).not.toContain(ko.fleet.nodes.columns.arch);
    expect(headers()).not.toContain(ko.fleet.nodes.columns.meshIp);
    expect(headers()).not.toContain(ko.fleet.nodes.columns.probe);
    fireEvent.click(screen.getByRole('checkbox', { name: ko.fleet.nodes.detailColumns }));
    expect(headers()).toEqual(expect.arrayContaining([ko.fleet.nodes.columns.arch, ko.fleet.nodes.columns.meshIp, ko.fleet.nodes.columns.probe]));
    unmount();
    renderPage();
    await screen.findByText('34.1');
    expect(headers()).toContain(ko.fleet.nodes.columns.arch);
    expect((screen.getByRole('checkbox', { name: ko.fleet.nodes.detailColumns }) as HTMLInputElement).checked).toBe(true);
  });

  it('links the accept-new chip to the node page', async () => {
    mocked.fetchServers.mockResolvedValue([{ ...servers[0], connection: 'fleet_file', host_key_mode: 'accept-new' }, servers[1]]);
    renderPage();
    const chip = (await screen.findAllByText('accept-new'))[0];
    expect(chip.closest('a')?.getAttribute('href')).toBe('#/fleet/nodes/a1');
  });

  it('probes the fleet on open and shows reachability from the probe, not the config', async () => {
    renderPage();
    expect(await screen.findByText('3.46')).toBeTruthy();
    expect(screen.getAllByText(ko.common.durationD(3)).length).toBeGreaterThan(0);
    const inventory = screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') });
    expect(inventory.textContent).toContain(ko.shell.reachability.offline);
    expect(screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') }).textContent).toContain(ko.shell.reachability.ok);
    expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.rerunDiagnosis }));
    await waitFor(() => expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(2));
  });

  it('shows the hub reachability class instead of a bare offline', async () => {
    mocked.diagnoseFleet.mockResolvedValue({
      ...diagnosis,
      results: diagnosis.results.map((r) => (r.id === 'b2' ? { ...r, reachability: 'host_key_changed' as const } : { ...r, reachability: 'command_failed' as const })),
    });
    renderPage();
    expect((await screen.findAllByText(ko.shell.reachability.hostKeyChanged)).length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') }).textContent).toContain(ko.shell.reachability.commandFailed);
  });

  it('merges the probe into one inventory table with no separate diagnosis table', async () => {
    renderPage();
    const table = await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') });
    fireEvent.click(screen.getByRole('checkbox', { name: ko.fleet.nodes.detailColumns }));
    await within(table).findByText('4G/8G');
    const cells = Array.from(table.querySelectorAll('tbody tr')[0].querySelectorAll('td')).map((c) => c.textContent);
    // The first cell is the selection checkbox (its label is an aria-label, so it has no text).
    expect(cells).toEqual(['', 'site-a-arm1', '10.0.0.1', 'arm64', '100.64.0.1', 'Site A, ARM', `${ko.shell.reachability.ok}${ko.shell.reachability.ok}`, ko.common.durationD(3), '0.1 0.2 0.3', '4G/8G', '48%', '120 ms']);
    // The offline node shows dashes, not invented values.
    const siteB = screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') });
    expect(Array.from(siteB.querySelectorAll('tbody tr')[0].querySelectorAll('td')).slice(6).map((c) => c.textContent)).toEqual([`${ko.shell.reachability.offline}${ko.shell.reachability.offline}`, '—', '—', '—', '—', '2000 ms']);
    expect(screen.queryByRole('table', { name: 'Fleet diagnosis results' })).toBeNull();
    expect(screen.getByText(ko.fleet.nodes.nodesOnline)).toBeTruthy();
    expect(screen.getByText(new RegExp(`^${ko.fleet.nodes.probedPrefix}(${ko.common.justNow}|\\d+\\S+ 전)$`))).toBeTruthy();
  });

  it('links node names to their detail page', async () => {
    renderPage();
    const table = await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') });
    expect(within(table).getByRole('link', { name: 'site-b-ec1' }).getAttribute('href')).toBe('#/fleet/nodes/b2');
  });

  it('puts the hub host after the inventory and no longer manages groups here', async () => {
    renderPage();
    await screen.findByText('34.1');
    const titles = Array.from(document.querySelectorAll('h2, h3')).map((h) => h.textContent ?? '');
    const at = (t: string) => titles.findIndex((x) => x.startsWith(t));
    expect(at(ko.fleet.nodes.inventoryPanel())).toBeLessThan(at(ko.fleet.nodes.hub));
    expect(at(ko.fleet.groupsSection.title())).toBe(-1);
  });

  it('reads ?status=offline from the URL', async () => {
    window.location.hash = '#/fleet/nodes?status=offline';
    renderPage();
    expect(await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeTruthy();
    expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') })).toBeNull();
    expect((screen.getByLabelText(ko.fleet.nodes.statusFilter) as HTMLSelectElement).value).toBe('offline');
  });

  it('reads ?status=online and ?group= together', async () => {
    window.location.hash = '#/fleet/nodes?status=online&group=arm';
    renderPage();
    expect(await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('ARM') })).toBeTruthy();
    expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeNull();

    window.location.hash = '#/fleet/nodes?status=offline&group=arm';
    expect(await screen.findByText(ko.fleet.nodes.emptyStatus('offline', true))).toBeTruthy();
  });

  it('does not filter by status until a probe exists, and says so', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('probe timed out'));
    window.location.hash = '#/fleet/nodes?status=offline';
    renderPage();
    expect(await screen.findByText(ko.fleet.nodes.runDiagnosisToFilter)).toBeTruthy();
    expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeNull();
  });

  it('ignores an unknown status value', async () => {
    window.location.hash = '#/fleet/nodes?status=bogus';
    renderPage();
    expect(await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') })).toBeTruthy();
    expect(screen.getByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeTruthy();
  });

  it('writes the filters into the URL when the controls change', async () => {
    renderPage();
    await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') });
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.statusFilter), { target: { value: 'online' } });
    expect(window.location.hash).toBe('#/fleet/nodes?status=online');
    await waitFor(() => expect(screen.queryByRole('table', { name: ko.fleet.nodes.nodesAria('Site B') })).toBeNull());
    fireEvent.change(screen.getByLabelText(ko.fleet.nodes.groupFilter), { target: { value: 'arm' } });
    expect(window.location.hash).toBe('#/fleet/nodes?status=online&group=arm');
    await waitFor(() => expect((screen.getByLabelText(ko.fleet.nodes.groupFilter) as HTMLSelectElement).value).toBe('arm'));
  });

  it('says a server was not checked until a probe has run', async () => {
    mocked.diagnoseFleet.mockReturnValue(new Promise(() => undefined));
    renderPage();
    const table = await screen.findByRole('table', { name: ko.fleet.nodes.nodesAria('Site A') });
    expect(table.textContent).toContain(ko.fleet.nodes.reach.checking);
    expect(table.textContent).not.toContain(ko.shell.reachability.ok);
  });

  it('shows the diagnosis failure instead of a result', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('probe timed out'));
    renderPage();
    await screen.findByText('34.1');
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.runDiagnosis }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('probe timed out');
    expect(screen.queryByText(ko.fleet.nodes.probeDuration)).toBeNull();
  });

  it('shows the hub error with a retry and no numbers when metrics fail', async () => {
    mocked.fetchMetrics.mockRejectedValueOnce(new api.HubError('metrics backend down', 500));
    renderPage();
    const alert = await screen.findByText('metrics backend down');
    expect(alert).toBeTruthy();
    expect(screen.queryByText('34.1')).toBeNull();
    expect(screen.queryByRole('meter')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: ko.common.retry }));
    expect(await screen.findByText('34.1')).toBeTruthy();
    expect(screen.queryByText('metrics backend down')).toBeNull();
  });

  it('shows the server-list error separately', async () => {
    mocked.fetchServers.mockRejectedValue(new Error('inventory unavailable'));
    renderPage();
    await waitFor(() => expect(screen.getByText('inventory unavailable')).toBeTruthy());
    expect(screen.getByText('34.1')).toBeTruthy();
  });
});

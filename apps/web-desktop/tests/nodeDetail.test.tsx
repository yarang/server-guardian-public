import { fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, fleetSSH, groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { NodeDetailPage } from '../src/pages/NodeDetailPage';
import { getDebatePrefill } from '../src/state/debatePrefill';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const renderPage = (id: string) =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <NodeDetailPage id={id} />
      </HubStoreProvider>
    </GuardianRoot>
  );

describe('NodeDetailPage', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
  });

  it('shows the node header, its group memberships, the shared probe result and its SSH audit', async () => {
    renderPage('a1');
    const header = await screen.findByRole('region', { name: 'site-a-arm1' });
    expect(await within(header).findByText('Site A, ARM')).toBeTruthy();
    expect(header.textContent).toContain('10.0.0.1');
    expect(header.textContent).toContain('arm64');
    expect(header.textContent).toContain('100.64.0.1');

    const probe = await screen.findByRole('region', { name: ko.fleet.detail.probe.title });
    expect(await within(probe).findByText(ko.shell.reachability.ok)).toBeTruthy();
    expect(probe.textContent).toContain('up 3 days');
    expect(probe.textContent).toContain('0.1 0.2 0.3');
    expect(probe.textContent).toContain('4G/8G');
    expect(probe.textContent).toContain('48%');
    expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(1);

    const ssh = await screen.findByRole('region', { name: ko.fleet.detail.sshAudit });
    expect(await within(ssh).findByText('Disable Direct Root Login via SSH')).toBeTruthy();
    expect(within(ssh).getByText('85')).toBeTruthy();
    // Audits only this node, from the hub cache.
    expect(mocked.fetchFleetSSHAudit).toHaveBeenCalledWith(['a1'], false);
  });

  it('shows an offline node as offline', async () => {
    renderPage('b2');
    const probe = await screen.findByRole('region', { name: ko.fleet.detail.probe.title });
    expect(await within(probe).findByText(ko.shell.reachability.offline)).toBeTruthy();
  });

  it('shows the actions: shell, run command and debate this node', async () => {
    renderPage('b2');
    const header = await screen.findByRole('region', { name: 'site-b-ec1' });
    expect(within(header).getByRole('link', { name: ko.fleet.detail.runOnNode }).getAttribute('href')).toBe('#/fleet/commands?target=b2');
    expect(within(header).getByRole('link', { name: ko.fleet.detail.backToNodes }).getAttribute('href')).toBe('#/fleet/nodes');

    await screen.findByText('Disable Direct Root Login via SSH');
    fireEvent.click(within(header).getByRole('button', { name: ko.fleet.detail.debateNode }));
    expect(window.location.hash).toBe('#/debates');
    const prefill = getDebatePrefill();
    expect(prefill?.source).toBe('node');
    expect(prefill?.task).toContain('node site-b-ec1 (b2)');
    expect(prefill?.task).toContain('Scope: this node only.');
    expect(prefill?.task).toContain('Probe: OFFLINE');
    expect(prefill?.task).toContain('SSH audit: score 40/100 (2 failed, 1 passed).');
    expect(prefill?.task).toContain('[HIGH] FAIL Disable Direct Root Login via SSH: current "yes", expected "no"');
  });

  it('says the node is unknown with a link back, and never audits it', async () => {
    renderPage('nope');
    expect(await screen.findByText(ko.fleet.detail.notFound('nope'))).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.fleet.detail.backToNodes }).getAttribute('href')).toBe('#/fleet/nodes');
    expect(mocked.fetchFleetSSHAudit).not.toHaveBeenCalled();
  });

  it('shows the inventory error with a link back', async () => {
    mocked.fetchServers.mockRejectedValue(new Error('inventory down'));
    renderPage('a1');
    expect(await screen.findByText('inventory down')).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.fleet.detail.backToNodes })).toBeTruthy();
  });

  it('reports an SSH audit failure without hiding the rest', async () => {
    mocked.fetchFleetSSHAudit.mockRejectedValue(new Error('audit exploded'));
    renderPage('a1');
    expect(await screen.findByText('audit exploded')).toBeTruthy();
    expect(screen.getByRole('region', { name: ko.fleet.detail.probe.title })).toBeTruthy();
  });

  it('keeps an unknown probe honest when the diagnosis failed', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('probe timed out'));
    renderPage('a1');
    expect(await screen.findByText('probe timed out')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: ko.fleet.detail.debateNode }));
    expect(getDebatePrefill()?.task).toContain('Probe: not available.');
  });
});

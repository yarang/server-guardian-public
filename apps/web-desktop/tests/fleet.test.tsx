import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import { diagnosis, keys, metrics, security, servers, sshConfig } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { App } from '../src/App';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const renderApp = () =>
  render(
    <GuardianRoot>
      <App />
    </GuardianRoot>
  );

const tab = (name: string) => screen.getByRole('button', { name });
const pressed = (name: string) => tab(name).getAttribute('aria-pressed');

beforeEach(() => {
  mocked.checkAgentHealth.mockResolvedValue({ state: 'online', reasons: [] });
  mocked.fetchMetrics.mockResolvedValue(metrics);
  mocked.fetchServers.mockResolvedValue(servers);
  mocked.fetchSSHSecurity.mockResolvedValue(security);
  mocked.fetchSSHConfig.mockResolvedValue(sshConfig);
  mocked.fetchAuthorizedKeys.mockResolvedValue(keys);
  mocked.diagnoseFleet.mockResolvedValue(diagnosis);
  mocked.fetchPersonas.mockResolvedValue(personaList);
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.listActions.mockResolvedValue([]);
});

describe('Fleet tabs', () => {
  it('lists Summary, Nodes, SSH audit, Commands and Groups as a tab group', () => {
    window.location.hash = '#/fleet';
    renderApp();
    const group = screen.getByRole('group', { name: ko.fleet.nodes.fleetSections });
    expect(within(group).getAllByRole('button').map((b) => b.textContent)).toEqual([ko.shell.nav.fleetTabs.summary, ko.shell.nav.fleetTabs.nodes, ko.shell.nav.fleetTabs.ssh, ko.shell.nav.fleetTabs.commands, ko.shell.nav.fleetTabs.groups]);
  });

  it('shows the summary at #/fleet', async () => {
    window.location.hash = '#/fleet';
    renderApp();
    expect(pressed(ko.shell.nav.fleetTabs.summary)).toBe('true');
    expect(await screen.findByRole('region', { name: ko.ops.overview.reachability.title })).toBeTruthy();
    expect(screen.queryByText(ko.fleet.nodes.inventoryPanel(2))).toBeNull();
  });

  it('shows the inventory at #/fleet/nodes, the node page at #/fleet/nodes/<id> and the wizard at #/fleet/nodes/new', async () => {
    window.location.hash = '#/fleet/nodes';
    renderApp();
    expect(await screen.findByText(ko.fleet.nodes.inventoryPanel(2))).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.nodes)).toBe('true');
    expect(pressed(ko.shell.nav.fleetTabs.summary)).toBe('false');

    window.location.hash = '#/fleet/nodes/a1';
    expect(await screen.findByRole('region', { name: 'site-a-arm1' })).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.nodes)).toBe('true');

    window.location.hash = '#/fleet/nodes/new';
    expect(await screen.findByRole('region', { name: ko.fleet.register.addNode })).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.nodes)).toBe('true');
  });

  it('shows the SSH audit at #/fleet/ssh', async () => {
    window.location.hash = '#/fleet/ssh';
    renderApp();
    expect(await screen.findByText(ko.fleet.ssh.hubAgentSummary)).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.ssh)).toBe('true');
  });

  it('shows the commands at #/fleet/commands and keeps ?target= and ?group= from the URL', async () => {
    window.location.hash = '#/fleet/commands?target=b2';
    renderApp();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    expect(pressed(ko.shell.nav.fleetTabs.commands)).toBe('true');
    await waitFor(() => expect((screen.getByLabelText(ko.ops.run.target) as HTMLSelectElement).value).toBe('b2'));
  });

  it('writes the selected tab into the URL', async () => {
    window.location.hash = '#/fleet';
    renderApp();
    await screen.findByRole('region', { name: ko.ops.overview.reachability.title });

    fireEvent.click(tab(ko.shell.nav.fleetTabs.nodes));
    expect(window.location.hash).toBe('#/fleet/nodes');
    expect(await screen.findByText(ko.fleet.nodes.inventoryPanel(2))).toBeTruthy();

    fireEvent.click(tab(ko.shell.nav.fleetTabs.ssh));
    expect(window.location.hash).toBe('#/fleet/ssh');
    expect(await screen.findByText(ko.fleet.ssh.hubAgentSummary)).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.ssh)).toBe('true');

    fireEvent.click(tab(ko.shell.nav.fleetTabs.commands));
    expect(window.location.hash).toBe('#/fleet/commands');
    expect(await screen.findByRole('option', { name: ko.ops.run.targetHub })).toBeTruthy();

    fireEvent.click(tab(ko.shell.nav.fleetTabs.summary));
    expect(window.location.hash).toBe('#/fleet');
    expect(await screen.findByRole('region', { name: ko.ops.overview.reachability.title })).toBeTruthy();
  });

  it('shows the summary for an unknown tab', async () => {
    window.location.hash = '#/fleet/bogus';
    renderApp();
    expect(await screen.findByRole('region', { name: ko.ops.overview.reachability.title })).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.summary)).toBe('true');
  });
});

describe('Links from the old routes still work', () => {
  it('opens the node page from #/nodes/<id> and the filtered inventory from #/nodes?status=offline', async () => {
    window.location.hash = '#/nodes/a1';
    renderApp();
    expect(await screen.findByRole('region', { name: 'site-a-arm1' })).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.nodes)).toBe('true');

    window.location.hash = '#/nodes?status=offline';
    expect(await screen.findByRole('link', { name: 'site-b-ec1' })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('link', { name: 'site-a-arm1' })).toBeNull());
  });

  it.each(['#/run?target=b2', '#/batch?target=b2', '#/terminal?target=b2'])('opens Commands with the target from %s', async (hash) => {
    window.location.hash = hash;
    renderApp();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    expect(pressed(ko.shell.nav.fleetTabs.commands)).toBe('true');
    await waitFor(() => expect((screen.getByLabelText(ko.ops.run.target) as HTMLSelectElement).value).toBe('b2'));
  });

  it.each(['#/security', '#/ssh'])('opens the SSH audit tab from %s', async (hash) => {
    window.location.hash = hash;
    renderApp();
    expect(await screen.findByText(ko.fleet.ssh.hubAgentSummary)).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.ssh)).toBe('true');
  });

  it.each(['#/overview', '#/assistant', '#/more'])('opens the Fleet summary from %s', async (hash) => {
    window.location.hash = hash;
    renderApp();
    expect(await screen.findByRole('region', { name: ko.ops.overview.reachability.title })).toBeTruthy();
    expect(pressed(ko.shell.nav.fleetTabs.summary)).toBe('true');
  });
});

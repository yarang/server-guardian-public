import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import { diagnosis, groupList, keys, metrics, security, servers, sshConfig } from './fixtures';
import type { FleetStreamEvent } from '../src/types';

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

function mockHealthyHub() {
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
}

const F0 = ko.shell.nav.routes.fleet.label;
const D0 = ko.shell.nav.routes.debates.label;
const S0 = ko.shell.nav.routes.settings.label;

describe('App shell', () => {
  beforeEach(() => {
    mockHealthyHub();
    mocked.checkAgentHealth.mockResolvedValue({ state: 'online', reasons: [] });
  });

  it('defaults to the Fleet summary and reports the agent online', async () => {
    renderApp();
    expect(screen.getByRole('heading', { level: 1, name: F0 })).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.nav.fleetTabs.summary }).getAttribute('aria-pressed')).toBe('true');
    expect(await screen.findByText(ko.shell.status.online)).toBeTruthy();
    expect(await screen.findByRole('region', { name: ko.ops.overview.reachability.title })).toBeTruthy();
  });

  it('reports a degraded agent with its reasons', async () => {
    mocked.checkAgentHealth.mockResolvedValue({ state: 'degraded', reasons: ['disk 93% full'] });
    renderApp();
    const chip = (await screen.findByText(ko.shell.status.degraded)).closest('[tabindex="0"]') as HTMLElement;
    expect(chip.getAttribute('title')).toBe('disk 93% full');
    // The reasons are visible text on hover and on keyboard focus, and hidden otherwise.
    expect(screen.queryByText('disk 93% full')).toBeNull();
    fireEvent.mouseEnter(chip);
    expect(screen.getByRole('tooltip').textContent).toBe('disk 93% full');
    fireEvent.mouseLeave(chip);
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.focus(chip);
    expect(screen.getByRole('tooltip').textContent).toBe('disk 93% full');
    fireEvent.blur(chip);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('says sign-in is required, not offline, when the session is missing or expired', async () => {
    mocked.checkAgentHealth.mockResolvedValue({ state: 'signed-out', reasons: [] });
    renderApp();
    const chip = await screen.findByText(ko.shell.status.signInRequired);
    expect(chip.className).toContain('sg-chip--idle');
    expect(screen.queryByText(ko.shell.status.offline)).toBeNull();
  });

  it('reports the agent offline', async () => {
    mocked.checkAgentHealth.mockResolvedValue({ state: 'offline', reasons: [] });
    renderApp();
    expect(await screen.findByText(ko.shell.status.offline)).toBeTruthy();
  });

  it('follows the URL hash for every route and links the navigation to hashes', async () => {
    const F = ko.shell.nav.routes.fleet.title;
    const D = ko.shell.nav.routes.debates.title;
    const A = ko.shell.nav.routes.approvals.title;
    const S = ko.shell.nav.routes.settings.title;
    const cases: Array<[string, string]> = [
      ['#/fleet', F],
      ['#/fleet/nodes', F],
      ['#/fleet/ssh', F],
      ['#/fleet/commands', F],
      ['#/debates', D],
      ['#/approvals', A],
      ['#/settings', S],
      // legacy aliases land on the four destinations
      ['#/overview', F],
      ['#/nodes', F],
      ['#/security', F],
      ['#/ssh', F],
      ['#/run', F],
      ['#/batch', F],
      ['#/terminal', F],
      ['#/assistant', F],
      ['#/more', F],
      ['#/agents', D],
      ['#/debate', D],
      ['#/unknown', F],
    ];
    renderApp();
    for (const [hash, title] of cases) {
      window.location.hash = hash;
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(title));
    }

    const nav = screen.getByRole('navigation', { name: ko.shell.nav.primary });
    const link = within(nav).getByRole('link', { name: D0 });
    expect(link.getAttribute('href')).toBe('#/debates');
    window.location.hash = '#/debates';
    await waitFor(() => expect(within(nav).getByRole('link', { name: D0 }).getAttribute('aria-current')).toBe('page'));
    // Every Fleet page keeps Fleet current.
    window.location.hash = '#/nodes/a1';
    await waitFor(() => expect(within(nav).getByRole('link', { name: F0 }).getAttribute('aria-current')).toBe('page'));
  });

  it('lists the same four destinations in the sidebar and the mobile tab bar, with no More tab', () => {
    renderApp();
    const nav = screen.getByRole('navigation', { name: ko.shell.nav.primary });
    // One flat list: no group headings.
    expect(within(nav).queryAllByRole('group')).toHaveLength(0);
    const names = [F0, D0, ko.shell.nav.routes.approvals.label, S0];
    const hrefs = ['#/fleet', '#/debates', '#/approvals', '#/settings'];
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(names);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(hrefs);

    const tabs = screen.getByRole('navigation', { name: ko.shell.nav.primaryMobile });
    const tabLinks = within(tabs).getAllByRole('link');
    expect(tabLinks.map((a) => a.textContent)).toEqual(names);
    expect(tabLinks.map((a) => a.getAttribute('href'))).toEqual(hrefs);
    expect(within(tabs).queryByRole('link', { name: '더보기' })).toBeNull();
  });

  it('marks the current destination on the mobile tab bar', async () => {
    window.location.hash = '#/settings';
    renderApp();
    const tabs = screen.getByRole('navigation', { name: ko.shell.nav.primaryMobile });
    await waitFor(() => expect(within(tabs).getByRole('link', { name: S0 }).getAttribute('aria-current')).toBe('page'));
    expect(within(tabs).getByRole('link', { name: F0 }).getAttribute('aria-current')).toBeNull();
    window.location.hash = '#/run';
    await waitFor(() => expect(within(tabs).getByRole('link', { name: F0 }).getAttribute('aria-current')).toBe('page'));
  });

  it('renders the legacy terminal alias as the Commands tab', async () => {
    window.location.hash = '#/terminal';
    renderApp();
    expect(await screen.findByRole('option', { name: ko.ops.run.targetHub })).toBeTruthy();
  });

  it('renders the agent endpoint on the Settings page and opens the dialog from it', async () => {
    window.location.hash = '#/settings';
    renderApp();
    fireEvent.click(await screen.findByRole('button', { name: ko.shell.settingsPage.changeEndpoint }));
    expect(await screen.findByRole('dialog', { name: ko.shell.settingsDialog.title })).toBeTruthy();
  });

  it('probes the fleet once across navigation and again on Refresh', async () => {
    renderApp();
    await screen.findByRole('region', { name: ko.ops.overview.reachability.title });
    expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(1);

    window.location.hash = '#/fleet/nodes';
    await screen.findByText(ko.fleet.nodes.inventoryPanel(2));
    window.location.hash = '#/fleet';
    await screen.findByRole('region', { name: ko.ops.overview.reachability.title });
    expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(1);
    expect(mocked.fetchServers).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: ko.shell.header.refresh }));
    await waitFor(() => expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(2));
  });

  it('routes #/nodes/:id to the node detail and #/nodes?status= to the filtered inventory', async () => {
    window.location.hash = '#/fleet/nodes/a1';
    renderApp();
    expect(await screen.findByRole('region', { name: 'site-a-arm1' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(F0);
    expect(screen.getByRole('button', { name: ko.shell.nav.fleetTabs.nodes }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('link', { name: ko.fleet.detail.runOnNode }).getAttribute('href')).toBe('#/fleet/commands?target=a1');

    window.location.hash = '#/fleet/nodes?status=offline';
    expect(await screen.findByRole('link', { name: 'site-b-ec1' })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('link', { name: 'site-a-arm1' })).toBeNull());
  });

  it('opens Run with the node from ?target= preselected (a stale ?mode=terminal is ignored)', async () => {
    window.location.hash = '#/fleet/commands?mode=terminal&target=b2';
    renderApp();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    await waitFor(() => expect((screen.getByLabelText(ko.ops.run.target) as HTMLSelectElement).value).toBe('b2'));
  });

  it('keeps a fleet run alive across pages and flags it on the Fleet nav item', async () => {
    let emit: (e: FleetStreamEvent) => void = () => undefined;
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.streamFleetExec.mockImplementation((_cmd, _targets, onEvent) => {
      emit = (e) => act(() => onEvent(e));
      return () => undefined;
    });
    window.location.hash = '#/fleet/commands';
    renderApp();
    await screen.findByRole('option', { name: 'site-b-ec1 (10.0.0.2)' });
    const nav = screen.getByRole('navigation', { name: ko.shell.nav.primary });
    expect(within(nav).getByRole('link', { name: F0 })).toBeTruthy();

    fireEvent.change(screen.getByLabelText(ko.ops.run.target), { target: { value: 'all' } });
    const input = screen.getByLabelText(ko.shell.ui.terminal.command) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'uptime' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    fireEvent.click(await screen.findByRole('button', { name: ko.ops.run.confirm.confirmFleet }));
    expect(within(nav).getByRole('link', { name: `${F0} ${ko.common.running}` })).toBeTruthy();
    expect(within(screen.getByRole('navigation', { name: ko.shell.nav.primaryMobile })).getByRole('link', { name: `${F0} ${ko.common.running}` })).toBeTruthy();

    window.location.hash = '#/fleet/nodes';
    await screen.findByText(ko.fleet.nodes.inventoryPanel(2));
    expect(within(nav).getByRole('link', { name: `${F0} ${ko.common.running}` })).toBeTruthy();
    expect(mocked.streamFleetExec).toHaveBeenCalledTimes(1);

    emit({ type: 'complete', success: 2, failed: 0, elapsed_sec: 1 });
    await waitFor(() => expect(within(nav).getByRole('link', { name: F0 })).toBeTruthy());

    window.location.hash = '#/fleet/commands';
    expect(await screen.findByText(ko.ops.run.finished)).toBeTruthy();
    expect((screen.getByLabelText(ko.ops.run.target) as HTMLSelectElement).value).toBe('all');
  });

  it('renders only the debates list and the collapsed new-debate form at #/debates', async () => {
    window.location.hash = '#/debates';
    renderApp();
    expect(screen.queryByRole('button', { name: ko.debate.start.start })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.page.newDebate }));
    expect(screen.getByRole('button', { name: ko.debate.start.start })).toBeTruthy();
    expect(await screen.findByText(ko.debate.page.empty)).toBeTruthy();
    expect(screen.queryByText(new RegExp('^' + ko.debate.persona.section))).toBeNull();
    expect(screen.queryByText(new RegExp('^' + ko.debate.team.section))).toBeNull();
    expect(screen.getByRole('link', { name: ko.debate.page.manageTeams }).getAttribute('href')).toBe('#/settings/teams');
  });

  it('renders the proposed actions at #/approvals and the configuration at #/settings/teams', async () => {
    window.location.hash = '#/approvals';
    renderApp();
    expect(await screen.findByText(ko.debate.actions.empty)).toBeTruthy();
    window.location.hash = '#/settings/teams';
    expect(await screen.findByText(ko.debate.team.sectionCount(2))).toBeTruthy();
    window.location.hash = '#/settings/personas';
    expect(await screen.findByText(ko.debate.persona.sectionCount(4))).toBeTruthy();
  });

  it('Refresh refetches the page data', async () => {
    window.location.hash = '#/fleet/nodes';
    renderApp();
    await screen.findByText('34.1');
    const before = mocked.fetchMetrics.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: ko.shell.header.refresh }));
    await waitFor(() => expect(mocked.fetchMetrics.mock.calls.length).toBeGreaterThan(before));
  });

  it('opens the endpoint dialog, validates the URL and saves it', async () => {
    window.location.hash = '#/fleet/nodes';
    renderApp();
    await screen.findByText('34.1');
    fireEvent.click(screen.getByRole('button', { name: ko.shell.settingsDialog.title }));
    const dialog = await screen.findByRole('dialog', { name: ko.shell.settingsDialog.title });
    const input = within(dialog).getByLabelText(ko.shell.settingsDialog.urlLabel);
    expect((input as HTMLInputElement).value).toBe('http://hub.test:9870');

    fireEvent.change(input, { target: { value: 'not a url' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.shell.settingsDialog.saveAndReconnect }));
    expect(within(dialog).getByRole('alert').textContent).toContain('http(s) URL');
    expect(mocked.setAgentUrl).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'http://other:9870' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.shell.settingsDialog.saveAndReconnect }));
    expect(mocked.setAgentUrl).toHaveBeenCalledWith('http://other:9870');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('Login', () => {
  beforeEach(() => {
    mocked.checkAgentHealth.mockResolvedValue({ state: 'offline', reasons: [] });
    mocked.fetchMetrics.mockRejectedValue(new api.AuthRequiredError('authentication required'));
    mocked.fetchServers.mockRejectedValue(new api.AuthRequiredError('authentication required'));
  });

  it('opens the login dialog when the api throws AuthRequiredError', async () => {
    renderApp();
    const dialog = await screen.findByRole('dialog', { name: ko.shell.login.title });
    expect(within(dialog).getByLabelText(ko.shell.login.tokenLabel)).toBeTruthy();
  });

  it('signs in with the token and refetches; a wrong token shows an error and keeps the dialog', async () => {
    mocked.login.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    renderApp();
    const dialog = await screen.findByRole('dialog', { name: ko.shell.login.title });
    const input = within(dialog).getByLabelText(ko.shell.login.tokenLabel);

    fireEvent.change(input, { target: { value: ' bad ' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    expect(await within(dialog).findByText(ko.shell.login.invalid)).toBeTruthy();
    expect(mocked.login).toHaveBeenLastCalledWith('bad');

    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    window.location.hash = '#/fleet/nodes';
    fireEvent.change(input, { target: { value: 'good-token' } });
    fireEvent.submit(input.closest('form') as HTMLFormElement);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mocked.login).toHaveBeenLastCalledWith('good-token');
    expect(await screen.findByText('34.1')).toBeTruthy();
  });

  it('shows the target agent and can switch to the endpoint dialog', async () => {
    renderApp();
    const dialog = await screen.findByRole('dialog', { name: ko.shell.login.title });
    expect(within(dialog).getByText('http://hub.test:9870')).toBeTruthy();

    fireEvent.click(within(dialog).getByRole('button', { name: ko.shell.login.changeHub }));
    const endpoint = await screen.findByRole('dialog', { name: ko.shell.settingsDialog.title });
    expect(screen.queryByRole('dialog', { name: ko.shell.login.title })).toBeNull();
    fireEvent.change(within(endpoint).getByLabelText(ko.shell.settingsDialog.urlLabel), { target: { value: 'http://100.64.0.7:9870' } });
    fireEvent.click(within(endpoint).getByRole('button', { name: ko.shell.settingsDialog.saveAndReconnect }));
    expect(mocked.setAgentUrl).toHaveBeenCalledWith('http://100.64.0.7:9870');
  });

  it('sign out from the endpoint dialog calls logout and asks for login again', async () => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    window.location.hash = '#/fleet/nodes';
    renderApp();
    await screen.findByText('34.1');
    fireEvent.click(screen.getByRole('button', { name: ko.shell.settingsDialog.title }));
    const dialog = await screen.findByRole('dialog', { name: ko.shell.settingsDialog.title });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.shell.settingsDialog.signOut }));
    expect(await screen.findByRole('dialog', { name: ko.shell.login.title })).toBeTruthy();
    expect(mocked.logout).toHaveBeenCalled();
  });
});

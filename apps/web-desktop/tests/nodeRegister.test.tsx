import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { NodeRegisterPage } from '../src/pages/NodeRegisterPage';
import type { NodeCheckResult, NodeWriteResult } from '../src/types';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);

const FP_ED = 'SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk';
const FP_ECDSA = 'SHA256:L+5UBANOVSvPSFxRbAg3IvTiKCDBqHV66Pu4fG4X/D0';
const AUTH_LINE = 'from="100.64.0.7",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nza guardian-hub-fleet@site-a-arm1';

const checked: NodeCheckResult = {
  ok: true,
  id: 'edge-1',
  hostname: '203.0.113.9',
  port: 22,
  id_exists: false,
  keys: [
    { type: 'ssh-ed25519', fingerprint: FP_ED },
    { type: 'ecdsa-sha2-nistp256', fingerprint: FP_ECDSA },
  ],
};

const node = {
  id: 'edge-1', name: 'edge-1', hostname: '203.0.113.9', user: 'ubuntu', port: 22, arch: 'aarch64', group: '', key: '', recommended: false,
  description: 'Edge box', tier: 'Tier-2', role: 'ssh_target_node', guardian_role: 'node', cli_support: ['ssh_agent_remote_exec'],
  tailscale_tags: ['tag:fleet'], connection: 'fleet_file' as const, host_key_mode: 'pinned' as const,
};

const registered = (probe: NodeWriteResult['probe'], extra: Partial<NodeWriteResult> = {}): NodeWriteResult => ({ ok: true, node, probe, ...extra });

const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <NodeRegisterPage />
      </HubStoreProvider>
    </GuardianRoot>
  );

const F = ko.fleet.forms;
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

/** Fills the connection step and gets a confirmed fingerprint. */
async function confirmHostKey() {
  type(F.connection.id, 'edge-1');
  type(F.connection.hostName, '203.0.113.9');
  fireEvent.click(screen.getByRole('button', { name: F.hostKey.check }));
  await screen.findByText(FP_ED);
  fireEvent.click(screen.getAllByRole('radio')[0]);
  fireEvent.click(screen.getByLabelText(F.hostKey.confirm));
}

describe('NodeRegisterPage', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/home/ubuntu/.ssh/guardian_fleet.conf', unmanaged_hosts: [], blockers: [], can_register: true });
    mocked.checkNode.mockResolvedValue(checked);
    mocked.probeNode.mockReset();
    mocked.registerNode.mockReset();
  });

  it('registers a node: check, confirm the fingerprint, metadata, register, probe ok', async () => {
    mocked.registerNode.mockResolvedValue(registered({ status: 'ok', arch: 'aarch64', duration_ms: 412 }));
    renderPage();
    await screen.findByLabelText(F.connection.id);
    expect((screen.getByLabelText(F.connection.user) as HTMLInputElement).value).toBe('ubuntu');
    expect((screen.getByLabelText(F.connection.port) as HTMLInputElement).value).toBe('22');
    expect(screen.getByText('/home/ubuntu/.ssh/guardian_fleet.conf')).toBeTruthy();

    type(F.connection.id, 'edge-1');
    type(F.connection.hostName, '203.0.113.9');
    const next = screen.getByRole('button', { name: ko.fleet.register.nextMetadata }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: F.hostKey.check }));
    expect(await screen.findByText(FP_ED)).toBeTruthy();
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'edge-1', hostname: '203.0.113.9', user: 'ubuntu', port: 22 });
    expect(screen.getByText('ssh-ed25519')).toBeTruthy();
    expect(screen.getByText('ecdsa-sha2-nistp256')).toBeTruthy();
    expect(screen.getByText('ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub')).toBeTruthy();

    // A fingerprint must be picked and explicitly confirmed before the wizard goes on.
    expect(next.disabled).toBe(true);
    fireEvent.click(screen.getAllByRole('radio')[0]);
    expect(next.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(F.hostKey.confirm));
    expect(next.disabled).toBe(false);
    fireEvent.click(next);

    // Metadata: guardian_role is fixed and the catalog values are offered as suggestions.
    const role = screen.getByLabelText(F.metadata.guardianRole) as HTMLInputElement;
    expect(role.value).toBe('node');
    expect(role.disabled).toBe(true);
    type(F.metadata.tier, 'Tier-2');
    type(F.metadata.role, 'ssh_target_node');
    type(F.metadata.cliSupport, 'ssh_agent_remote_exec');
    fireEvent.click(screen.getByRole('button', { name: F.list.addAria(F.metadata.cliSupport) }));
    type(F.metadata.tailscaleTags, 'tag:fleet');
    fireEvent.click(screen.getByRole('button', { name: F.list.addAria(F.metadata.tailscaleTags) }));
    type(F.metadata.description, 'Edge box');
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));

    expect(screen.getByText(FP_ED)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));
    expect(await screen.findByText(new RegExp(`${F.probe.arch('aarch64')} · 412 ms`))).toBeTruthy();
    expect(mocked.registerNode).toHaveBeenCalledWith({
      id: 'edge-1',
      hostname: '203.0.113.9',
      user: 'ubuntu',
      port: 22,
      host_key_fingerprint: FP_ED,
      metadata: {
        guardian_role: 'node',
        tier: 'Tier-2',
        role: 'ssh_target_node',
        cli_support: ['ssh_agent_remote_exec'],
        tailscale_tags: ['tag:fleet'],
        description: 'Edge box',
      },
    });
    expect(screen.getByRole('link', { name: ko.fleet.register.openNodePage }).getAttribute('href')).toBe('#/fleet/nodes/edge-1');
    // Servers and groups (rule groups may gain the node) are reloaded.
    await waitFor(() => expect(mocked.fetchServers).toHaveBeenCalledTimes(2));
    expect(mocked.fetchGroups).toHaveBeenCalledTimes(2);

    // Add another starts over.
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.addAnother }));
    expect((screen.getByLabelText(F.connection.id) as HTMLInputElement).value).toBe('');
  });

  it('stops with the blockers, the Include line and the fleet file path when the hub cannot register', async () => {
    mocked.fetchNodeRegistration.mockResolvedValue({
      fleet_file: '/home/ubuntu/.ssh/guardian_fleet.conf',
      ssh_config_file: '/home/ubuntu/.ssh/config',
      include_line: 'Include ~/.ssh/guardian_fleet.conf',
      include_effective: false,
      inventory_writable: false,
      inventory_file: '/home/ubuntu/.guardian/data/servers.json',
      blockers: ['add "Include ~/.ssh/guardian_fleet.conf" at the top of /home/ubuntu/.ssh/config', 'GUARDIAN_SERVERS_FILE is not set'],
      can_register: false,
    });
    renderPage();
    expect(await screen.findByText('GUARDIAN_SERVERS_FILE is not set')).toBeTruthy();
    expect(screen.getByText(ko.fleet.register.includeCaption('/home/ubuntu/.ssh/config'))).toBeTruthy();
    expect(screen.getByText('Include ~/.ssh/guardian_fleet.conf')).toBeTruthy();
    expect(screen.getByText('/home/ubuntu/.ssh/guardian_fleet.conf')).toBeTruthy();
    expect(screen.queryByLabelText(F.connection.id)).toBeNull();
    expect(screen.queryByRole('button', { name: F.hostKey.check })).toBeNull();
  });

  it('checks the fields in the browser first and keeps Check disabled while they are invalid', async () => {
    renderPage();
    await screen.findByLabelText(F.connection.id);
    type(F.connection.id, 'Bad_ID');
    type(F.connection.hostName, '-evil');
    type(F.connection.user, 'Root');
    type(F.connection.port, '70000');
    expect(screen.getByText(ko.fleet.validation.id)).toBeTruthy();
    expect(screen.getByText(ko.fleet.validation.hostFormat)).toBeTruthy();
    expect(screen.getByText(ko.fleet.validation.user)).toBeTruthy();
    expect(screen.getByText(ko.fleet.validation.port)).toBeTruthy();
    expect((screen.getByRole('button', { name: F.hostKey.check }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocked.checkNode).not.toHaveBeenCalled();
    // Words the API paths and dashboard routes use are refused, like the hub does.
    for (const word of ['new', 'registration', 'check', 'probe']) {
      type(F.connection.id, word);
      expect(screen.getByText(ko.fleet.validation.idReserved(word))).toBeTruthy();
    }
    type(F.connection.id, 'newer');
    expect(screen.queryByText(/예약어/)).toBeNull();
  });

  it('puts a hub field error on the matching input', async () => {
    mocked.checkNode.mockRejectedValue(
      new api.HubError('hostname: a name whose last label is all digits is not allowed', 400, { code: 'invalid_field', field: 'hostname' })
    );
    renderPage();
    await screen.findByLabelText(F.connection.id);
    type(F.connection.id, 'edge-1');
    type(F.connection.hostName, 'example.com');
    fireEvent.click(screen.getByRole('button', { name: F.hostKey.check }));
    expect(await screen.findByText('hostname: a name whose last label is all digits is not allowed')).toBeTruthy();
    expect(screen.getByLabelText(F.connection.hostName).getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText(F.connection.id).getAttribute('aria-invalid')).toBeNull();
    // Editing the field drops the hub's complaint.
    type(F.connection.hostName, 'example.org');
    expect(screen.queryByText(/last label is all digits/)).toBeNull();
  });

  it('shows an unreachable check as a notice with the hub hint', async () => {
    mocked.checkNode.mockRejectedValue(new api.HubError('no host key received', 502, { code: 'unreachable', hint: 'ssh-keyscan: connection timed out' }));
    renderPage();
    await screen.findByLabelText(F.connection.id);
    type(F.connection.id, 'edge-1');
    type(F.connection.hostName, '203.0.113.9');
    fireEvent.click(screen.getByRole('button', { name: F.hostKey.check }));
    const alert = await screen.findByText(/no host key received/);
    expect(alert.textContent).toContain('ssh-keyscan: connection timed out');
  });

  it('blocks an id that already exists in the inventory', async () => {
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await waitFor(() => expect(mocked.fetchServers).toHaveBeenCalled());
    type(F.connection.id, 'a1');
    await screen.findByText(ko.fleet.register.idExists);
  });

  it('shows the hub blocker instead of a missing key line when the hub address is unknown', async () => {
    mocked.registerNode.mockResolvedValue(
      registered({ status: 'key_not_installed', stderr: 'Permission denied (publickey).', duration_ms: 90 }, { authorized_key: '', authorized_key_blocker: 'Set the hub Tailscale address first.' })
    );
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));
    expect(await screen.findByText('Set the hub Tailscale address first.')).toBeTruthy();
    expect(screen.queryByText(F.probe.noKeyLine)).toBeNull();
  });

  it('invalidates the confirmation when the host changes after the check', async () => {
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    const next = screen.getByRole('button', { name: ko.fleet.register.nextMetadata }) as HTMLButtonElement;
    expect(next.disabled).toBe(false);
    type(F.connection.hostName, '203.0.113.10');
    expect(next.disabled).toBe(true);
    expect(screen.getByText(F.hostKey.stale)).toBeTruthy();
  });

  it('keeps the entered values when going back and forth', async () => {
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    type(F.metadata.tier, 'Tier-9');
    fireEvent.click(screen.getByRole('button', { name: ko.common.back }));
    expect((screen.getByLabelText(F.connection.id) as HTMLInputElement).value).toBe('edge-1');
    expect((screen.getByLabelText(F.hostKey.confirm) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    expect((screen.getByLabelText(F.metadata.tier) as HTMLInputElement).value).toBe('Tier-9');
  });

  it('shows the key line, the setup script and Test again when the key is not installed', async () => {
    mocked.registerNode.mockResolvedValue(registered({ status: 'key_not_installed', stderr: 'Permission denied (publickey).', duration_ms: 90 }, { authorized_key: AUTH_LINE }));
    mocked.probeNode.mockResolvedValue({ ok: true, id: 'edge-1', probe: { status: 'ok', arch: 'aarch64', sudo: 'no', duration_ms: 80 } });
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));

    expect(await screen.findByText(AUTH_LINE)).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.ui.copy })).toBeTruthy();
    expect(screen.getByText('scripts/setup-hub-ssh.sh edge-1')).toBeTruthy();
    expect(screen.getByText('Permission denied (publickey).')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: F.probe.testAgain }));
    expect(await screen.findByText(new RegExp(`${F.probe.arch('aarch64')} · 80 ms`))).toBeTruthy();
    expect(screen.getByText(F.probe.sudoNo).getAttribute('title')).toBe(F.probe.sudoNoHint);
    expect(mocked.probeNode).toHaveBeenCalledWith('edge-1');
    expect(screen.queryByText(AUTH_LINE)).toBeNull();
  });

  it('shows the stderr tail and Test again for an unreachable node, which stays registered', async () => {
    mocked.registerNode.mockResolvedValue(registered({ status: 'unreachable', stderr: 'ssh: connect to host 203.0.113.9 port 22: Connection timed out', duration_ms: 10000 }));
    mocked.probeNode.mockRejectedValue(new api.HubError('probe failed to start', 500));
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));
    expect(await screen.findByText('ssh: connect to host 203.0.113.9 port 22: Connection timed out')).toBeTruthy();
    expect(screen.getByText(F.probe.unreachable)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: F.probe.testAgain }));
    expect(await screen.findByText('probe failed to start')).toBeTruthy();
  });

  it('sends a metadata field error back to the metadata step', async () => {
    mocked.registerNode.mockRejectedValue(new api.HubError('metadata.tier: too long', 400, { code: 'invalid_field', field: 'metadata.tier' }));
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));
    await waitFor(() => expect(screen.getByLabelText(F.metadata.tier).getAttribute('aria-invalid')).toBe('true'));
    expect(screen.getAllByText('metadata.tier: too long').length).toBeGreaterThan(0);
  });

  it('asks again when the host presents other keys than the confirmed one', async () => {
    const NEW_FP = 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
    mocked.registerNode.mockRejectedValue(
      new api.HubError('the host now presents other keys', 409, { code: 'fingerprint_mismatch', keys: [{ type: 'ssh-ed25519', fingerprint: NEW_FP }] })
    );
    renderPage();
    await screen.findByLabelText(F.connection.id);
    await confirmHostKey();
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextMetadata }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.nextReview }));
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.register.registerNode }));
    expect(await screen.findByText(NEW_FP)).toBeTruthy();
    expect(screen.getByText('the host now presents other keys')).toBeTruthy();
    expect((screen.getByRole('button', { name: ko.fleet.register.nextMetadata }) as HTMLButtonElement).disabled).toBe(true);
    within(screen.getByRole('radiogroup', { name: F.hostKey.keysAria })).getAllByRole('radio');
  });
});

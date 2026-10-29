import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, fleetSSH, groupList, metrics, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { NodeDetailPage } from '../src/pages/NodeDetailPage';
import { NodesPage } from '../src/pages/NodesPage';
import type { NodeWriteResult, FleetNode } from '../src/types';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);

const FP = 'SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk';
const AUTH_LINE = 'from="100.64.0.7",no-agent-forwarding ssh-ed25519 AAAAC3Nza guardian-hub-fleet@site-a-arm1';

const fleetNode: FleetNode = {
  ...servers[0],
  tier: 'Tier-1',
  description: 'Site A, ARM',
  connection: 'fleet_file',
  host_key_mode: 'accept-new',
  cli_support: ['claude'],
};
const inventoryNode: FleetNode = { ...servers[1], connection: 'inventory' };
const hubNode: FleetNode = { ...servers[1], id: 'hub1', name: 'hub-main', guardian_role: 'hub', connection: 'fleet_file', host_key_mode: 'pinned' };

const renderDetail = (id: string) =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <NodeDetailPage id={id} />
      </HubStoreProvider>
    </GuardianRoot>
  );

const saved = (probe?: NodeWriteResult['probe'], extra: Partial<NodeWriteResult> = {}): NodeWriteResult => ({ ok: true, node: fleetNode, probe, ...extra });

async function openEdit(id = 'a1') {
  renderDetail(id);
  const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
  fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.editNode }));
  return screen.getByRole('dialog', { name: new RegExp(`^${ko.fleet.editNode.title('').trim()}`) });
}

describe('node detail: connection, edit, test and remove', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue([fleetNode, inventoryNode, hubNode]);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/f', unmanaged_hosts: [], blockers: [], can_register: true });
    mocked.checkNode.mockReset();
    mocked.updateNode.mockReset();
    mocked.deleteNode.mockReset();
    mocked.probeNode.mockReset();
  });

  it('shows the connection source and host key mode', async () => {
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    expect(panel.textContent).toContain(ko.fleet.detail.connectionLabel.fleet_file);
    expect(panel.textContent).toContain(ko.fleet.detail.hostKeyLabel['accept-new']);
  });

  it('shows an inventory node as not migrated with no host key mode', async () => {
    renderDetail('b2');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    expect(panel.textContent).toContain(ko.fleet.detail.connectionLabel.inventory);
    expect(within(panel).getByText(ko.fleet.detail.facts.hostKey).nextSibling?.textContent).toBe('—');
  });

  it('saves a user change without a host key check and shows the probe', async () => {
    mocked.updateNode.mockResolvedValue(saved({ status: 'ok', arch: 'arm64', duration_ms: 50 }));
    const dialog = await openEdit();
    const save = within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.user), { target: { value: 'deploy' } });
    expect(within(dialog).queryByRole('button', { name: ko.fleet.forms.hostKey.check })).toBeNull();
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(await within(dialog).findByText(new RegExp(`${ko.fleet.forms.probe.arch('arm64')} · 50 ms`))).toBeTruthy();
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { user: 'deploy' });
    await waitFor(() => expect(mocked.fetchServers).toHaveBeenCalledTimes(2));
  });

  it('requires a fresh check and a confirmed fingerprint when the hostname changes', async () => {
    mocked.checkNode.mockResolvedValue({ ok: true, id: 'a1', hostname: '198.51.100.4', port: 22, id_exists: true, keys: [{ type: 'ssh-ed25519', fingerprint: FP }] });
    mocked.updateNode.mockResolvedValue(saved({ status: 'key_not_installed', duration_ms: 9 }, { authorized_key: AUTH_LINE }));
    const dialog = await openEdit();
    const save = within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }) as HTMLButtonElement;
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.hostName), { target: { value: '198.51.100.4' } });
    expect(save.disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.hostKey.check }));
    // id_exists is informational for an existing node: no "already exists" error here.
    await within(dialog).findByText(FP);
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'a1', hostname: '198.51.100.4', user: 'u', port: 22 });
    expect(within(dialog).queryByText(/이미 있어요/)).toBeNull();
    expect(save.disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('radio'));
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.forms.hostKey.confirm));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(await within(dialog).findByText(AUTH_LINE)).toBeTruthy();
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { hostname: '198.51.100.4', host_key_fingerprint: FP });
  });

  it('requires a new check for a port change and drops the result when the host changes again', async () => {
    mocked.checkNode.mockResolvedValue({ ok: true, id: 'a1', hostname: '10.0.0.1', port: 2222, id_exists: true, keys: [{ type: 'ssh-ed25519', fingerprint: FP }] });
    const dialog = await openEdit();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.port), { target: { value: '2222' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.hostKey.check }));
    await within(dialog).findByText(FP);
    fireEvent.click(within(dialog).getByRole('radio'));
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.forms.hostKey.confirm));
    const save = within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.port), { target: { value: '2200' } });
    expect(save.disabled).toBe(true);
  });

  it('sends metadata changes alone, with cleared fields as null', async () => {
    mocked.updateNode.mockResolvedValue(saved());
    const dialog = await openEdit();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.metadata.tier), { target: { value: '' } });
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.metadata.description), { target: { value: 'Moved to ARM' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.list.removeAria(ko.fleet.forms.metadata.cliSupport, 'claude') }));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }));
    expect(await within(dialog).findByText(ko.fleet.editNode.onlyMetadata)).toBeTruthy();
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { metadata: { tier: null, description: 'Moved to ARM', cli_support: null } });
  });

  it('adds package roles, offers the roles file as suggestions and warns about a role it does not define', async () => {
    mocked.fetchPackageRoles.mockResolvedValue({
      ok: true,
      managers: ['apt'],
      roles: [{ name: 'web', description: 'Web server', protected: { apt: ['nginx*'] } }],
    });
    mocked.updateNode.mockResolvedValue(saved());
    const dialog = await openEdit();
    const field = ko.fleet.forms.metadata.opsRoles;
    fireEvent.change(within(dialog).getByLabelText(field), { target: { value: 'web' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.list.addAria(field) }));
    expect(dialog.querySelector('option[value="web"]')).not.toBeNull();
    fireEvent.change(within(dialog).getByLabelText(field), { target: { value: 'wbe' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.list.addAria(field) }));
    expect(await within(dialog).findByText(ko.fleet.validation.opsRoleUnknown('wbe'))).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.list.removeAria(field, 'wbe') }));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }));
    await waitFor(() => expect(mocked.updateNode).toHaveBeenCalled());
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { metadata: { ops_roles: ['web'] } });
  });

  it('needs a host key check for a first connection edit of an inventory node', async () => {
    const dialog = await openEdit('b2');
    expect(within(dialog).getByText(ko.fleet.editNode.inventory)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.user), { target: { value: 'deploy' } });
    expect(within(dialog).getByRole('button', { name: ko.fleet.forms.hostKey.check })).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('locks the connection fields of a hand-written block and shows the hub message on a 409', async () => {
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/f', unmanaged_hosts: ['a1'], blockers: [], can_register: true });
    const dialog = await openEdit();
    await waitFor(() => expect((within(dialog).getByLabelText(ko.fleet.forms.connection.hostName) as HTMLInputElement).disabled).toBe(true));
    expect(within(dialog).getByText(ko.fleet.editNode.locked)).toBeTruthy();
    // Metadata still saves.
    mocked.updateNode.mockResolvedValue(saved());
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.metadata.description), { target: { value: 'x' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }));
    await within(dialog).findByText(ko.fleet.editNode.saved('a1'));
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { metadata: { description: 'x' } });
  });

  it('shows the hub message and locks the fields when a save hits an unmanaged block', async () => {
    mocked.updateNode.mockRejectedValue(new api.HubError('the fleet file has a hand-edited block for a1', 409, { code: 'unmanaged_block', field: 'id' }));
    const dialog = await openEdit();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.user), { target: { value: 'deploy' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }));
    expect(await within(dialog).findByText(/the fleet file has a hand-edited block for a1/)).toBeTruthy();
    expect((within(dialog).getByLabelText(ko.fleet.forms.connection.user) as HTMLInputElement).disabled).toBe(true);
  });

  it('puts a hub field error from a save on its input', async () => {
    mocked.updateNode.mockRejectedValue(new api.HubError('metadata.tier: too long', 400, { code: 'invalid_field', field: 'metadata.tier' }));
    const dialog = await openEdit();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.metadata.tier), { target: { value: 'Tier-5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }));
    await waitFor(() => expect(within(dialog).getByLabelText(ko.fleet.forms.metadata.tier).getAttribute('aria-invalid')).toBe('true'));
  });

  it('tests the connection and shows the result', async () => {
    mocked.probeNode.mockResolvedValue({ ok: true, id: 'a1', probe: { status: 'unreachable', stderr: 'Connection refused', duration_ms: 5 } });
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.testConnection }));
    expect(await within(panel).findByText('Connection refused')).toBeTruthy();
    expect(mocked.probeNode).toHaveBeenCalledWith('a1');
  });

  it('removes a node only after its id is typed, then returns to Nodes', async () => {
    mocked.deleteNode.mockResolvedValue({ ok: true, id: 'a1', removed: { fleet_block: true, known_hosts: true, inventory: true } });
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.removeNode }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(ko.fleet.removeNode.title('').trim()) });
    expect(within(dialog).getByText(ko.fleet.removeNode.groupsWarning('a1'))).toBeTruthy();
    const confirm = within(dialog).getByRole('button', { name: ko.fleet.detail.removeNode }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a' } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    // The hub key is not revoked: the typed id alone is not enough.
    expect(confirm.disabled).toBe(true);
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.removeNode.skipRevoke));
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(window.location.hash).toBe('#/fleet/nodes'));
    expect(mocked.deleteNode).toHaveBeenCalledWith('a1');
    await waitFor(() => expect(mocked.fetchServers).toHaveBeenCalledTimes(2));
    expect(mocked.fetchGroups).toHaveBeenCalledTimes(2);
  });

  it('shows the hub error in the remove dialog and stays', async () => {
    mocked.deleteNode.mockRejectedValue(new api.HubError('the fleet file has a hand-edited block for a1', 409, { code: 'unmanaged_block' }));
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.removeNode }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(ko.fleet.removeNode.title('').trim()) });
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.removeNode.skipRevoke));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.detail.removeNode }));
    expect(await within(dialog).findByText(/hand-edited block/)).toBeTruthy();
    expect(window.location.hash).not.toBe('#/fleet/nodes');
  });

  it('puts revoke first: warns that the hub key stays and opens the revoke dialog from the remove dialog', async () => {
    mocked.fetchRevokeKeyPreview.mockResolvedValue({ ok: true, id: 'a1', keys: [], fleet_key: 'ssh-ed25519 AAAA hub' } as never);
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.removeNode }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(ko.fleet.removeNode.title('').trim()) });
    expect(within(dialog).getByText(ko.fleet.removeNode.needsRevoke)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.removeNode.revokeFirst }));
    expect(screen.queryByRole('dialog', { name: new RegExp(ko.fleet.removeNode.title('').trim()) })).toBeNull();
    expect(await screen.findByRole('dialog', { name: new RegExp(ko.fleet.revokeKey.title('').trim()) })).toBeTruthy();
  });

  it('shows the revoke time and needs no tick once the hub key is revoked', async () => {
    mocked.fetchServers.mockResolvedValue([{ ...fleetNode, hub_key_revoked_at: '2026-10-10T09:00:00Z' }, inventoryNode, hubNode]);
    mocked.deleteNode.mockResolvedValue({ ok: true, id: 'a1', removed: { fleet_block: true, known_hosts: true, inventory: true } });
    renderDetail('a1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.removeNode }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(ko.fleet.removeNode.title('').trim()) });
    expect(within(dialog).getByText(new RegExp(ko.fleet.removeNode.revoked('.*')))).toBeTruthy();
    expect(within(dialog).queryByRole('button', { name: ko.fleet.removeNode.revokeFirst })).toBeNull();
    expect(within(dialog).queryByLabelText(ko.fleet.removeNode.skipRevoke)).toBeNull();
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.removeNode.typeToConfirm('a1')), { target: { value: 'a1' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.detail.removeNode }));
    await waitFor(() => expect(mocked.deleteNode).toHaveBeenCalledWith('a1'));
  });

  it('cannot remove the hub', async () => {
    renderDetail('hub1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    expect((within(panel).getByRole('button', { name: ko.fleet.detail.removeNode }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(panel).getByText(ko.fleet.detail.hubNotice)).toBeTruthy();
  });
});

describe('node detail: pin or re-confirm the host key', () => {
  const tsNode: FleetNode = { ...servers[1], id: 'ts1', name: 'ts-node', hostname: '10.9.9.9', tailscale_ip: '100.64.0.9', connection: 'inventory' };
  const bareNode: FleetNode = { ...servers[1], id: 'bare', name: 'bare-node', hostname: 'bare.example.com', user: '', port: 0, tailscale_ip: '192.168.1.5', connection: 'inventory' };
  const pinnedNode: FleetNode = { ...fleetNode, id: 'p1', name: 'pinned-node', host_key_mode: 'pinned' };
  const keys = [{ type: 'ssh-ed25519', fingerprint: FP }];
  const checkOk = (hostname: string, port = 22) => ({ ok: true, id: 'x', hostname, port, id_exists: true, keys });

  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue([fleetNode, tsNode, bareNode, pinnedNode]);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/f', unmanaged_hosts: [], blockers: [], can_register: true });
    mocked.checkNode.mockReset();
    mocked.updateNode.mockReset();
  });

  async function openPin(id: string, label = ko.fleet.detail.pinHostKey) {
    renderDetail(id);
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    fireEvent.click(within(panel).getByRole('button', { name: label }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(`^${label}:`) });
    return dialog;
  }

  async function confirm(dialog: HTMLElement) {
    await within(dialog).findByText(FP);
    fireEvent.click(within(dialog).getByRole('radio'));
    fireEvent.click(within(dialog).getByLabelText(ko.fleet.forms.hostKey.confirm));
  }

  it('checks the current address, confirms, and PUTs only the fingerprint', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('10.0.0.1'));
    mocked.updateNode.mockResolvedValue({ ok: true, node: { ...fleetNode, host_key_mode: 'pinned' }, probe: { status: 'key_not_installed', duration_ms: 7 }, authorized_key: AUTH_LINE });
    const dialog = await openPin('a1');
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'a1', hostname: '10.0.0.1', user: 'u', port: 22 });
    const pin = within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }) as HTMLButtonElement;
    await within(dialog).findByText(FP);
    expect(pin.disabled).toBe(true);
    await confirm(dialog);
    expect(pin.disabled).toBe(false);
    fireEvent.click(pin);
    expect(await within(dialog).findByText(AUTH_LINE)).toBeTruthy();
    expect(mocked.updateNode).toHaveBeenCalledWith('a1', { host_key_fingerprint: FP });
    expect(Object.keys(mocked.updateNode.mock.calls[0][1])).toEqual(['host_key_fingerprint']);
    await waitFor(() => expect(mocked.fetchServers).toHaveBeenCalledTimes(2));
  });

  it('uses the Tailscale IP of an inventory node when it is in 100.64.x', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('100.64.0.9'));
    await openPin('ts1');
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'ts1', hostname: '100.64.0.9', user: 'u', port: 22 });
  });

  it('falls back to hostname, user ubuntu and port 22 for an inventory node without them', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('bare.example.com'));
    await openPin('bare');
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'bare', hostname: 'bare.example.com', user: 'ubuntu', port: 22 });
  });

  it('offers re-confirmation for a pinned node, less prominently', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('10.0.0.1'));
    mocked.updateNode.mockResolvedValue({ ok: true, node: pinnedNode, probe: { status: 'ok', arch: 'arm64', duration_ms: 3 } });
    renderDetail('p1');
    const panel = await screen.findByRole('region', { name: ko.fleet.detail.connection });
    expect(within(panel).queryByRole('button', { name: ko.fleet.detail.pinHostKey })).toBeNull();
    expect(within(panel).getByRole('button', { name: ko.fleet.detail.reconfirmHostKey }).className).toContain('sg-btn--ghost');
    fireEvent.click(within(panel).getByRole('button', { name: ko.fleet.detail.reconfirmHostKey }));
    const dialog = screen.getByRole('dialog', { name: new RegExp(`^${ko.fleet.detail.reconfirmHostKey}:`) });
    await confirm(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.pinKey.replacePinned }));
    expect(await within(dialog).findByText(new RegExp(`${ko.fleet.forms.probe.arch('arm64')} · 3 ms`))).toBeTruthy();
    expect(mocked.updateNode).toHaveBeenCalledWith('p1', { host_key_fingerprint: FP });
  });

  it('shows the keys the host presents now after a fingerprint_mismatch', async () => {
    const NEW_FP = 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
    mocked.checkNode.mockResolvedValue(checkOk('10.0.0.1'));
    mocked.updateNode.mockRejectedValue(new api.HubError('the host now presents other keys', 409, { code: 'fingerprint_mismatch', keys: [{ type: 'ssh-ed25519', fingerprint: NEW_FP }] }));
    const dialog = await openPin('a1');
    await confirm(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }));
    expect(await within(dialog).findByText(NEW_FP)).toBeTruthy();
    expect(within(dialog).getByText('the host now presents other keys')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('disables pinning and shows the message for an unmanaged block', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('10.0.0.1'));
    mocked.updateNode.mockRejectedValue(new api.HubError('the fleet file has a hand-edited block for a1', 409, { code: 'unmanaged_block' }));
    const dialog = await openPin('a1');
    await confirm(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }));
    expect(await within(dialog).findByText('the fleet file has a hand-edited block for a1')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('knows an unmanaged block before the first request', async () => {
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/f', unmanaged_hosts: ['a1'], blockers: [], can_register: true });
    mocked.checkNode.mockResolvedValue(checkOk('10.0.0.1'));
    const dialog = await openPin('a1');
    expect(await within(dialog).findByText(ko.fleet.pinKey.locked)).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: ko.fleet.detail.pinHostKey }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('explains a node with no usable address', async () => {
    mocked.checkNode.mockRejectedValue(new api.HubError('hostname: must be an IPv4 address or a DNS name', 400, { code: 'invalid_field', field: 'hostname' }));
    const dialog = await openPin('a1');
    expect(await within(dialog).findByText(ko.fleet.pinKey.noAddress('site-a-arm1'))).toBeTruthy();
  });

  it('keeps the first-connection fingerprint rule in Edit and scans the Tailscale IP for an inventory node', async () => {
    mocked.checkNode.mockResolvedValue(checkOk('100.64.0.9'));
    const dialog = await openEdit('ts1');
    fireEvent.change(within(dialog).getByLabelText(ko.fleet.forms.connection.user), { target: { value: 'deploy' } });
    expect((within(dialog).getByRole('button', { name: ko.fleet.editNode.saveChanges }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.fleet.forms.hostKey.check }));
    await within(dialog).findByText(FP);
    expect(mocked.checkNode).toHaveBeenCalledWith({ id: 'ts1', hostname: '100.64.0.9', user: 'deploy', port: 22 });
  });
});

describe('Nodes list: add node and badges', () => {
  beforeEach(() => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue([fleetNode, inventoryNode, hubNode]);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
  });

  it('links to the wizard and flags nodes that are not migrated or not pinned', async () => {
    render(
      <GuardianRoot>
        <HubStoreProvider>
          <NodesPage query={{}} />
        </HubStoreProvider>
      </GuardianRoot>
    );
    await screen.findAllByText('site-b-ec1');
    const inventoryBadge = screen.getByText('inventory');
    expect(inventoryBadge.getAttribute('title')).toContain('servers.json');
    // a1 is listed under two groups, so its badge shows twice; the pinned hub carries none.
    const accept = screen.getAllByText('accept-new');
    expect(accept).toHaveLength(2);
    expect(accept[0].closest('a')?.getAttribute('title')).toContain(ko.fleet.nodes.acceptNewTitle);
    fireEvent.click(screen.getByRole('button', { name: ko.fleet.nodes.addNode }));
    expect(window.location.hash).toBe('#/fleet/nodes/new');
  });
});

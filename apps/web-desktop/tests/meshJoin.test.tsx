import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { NodeRegisterPage } from '../src/pages/NodeRegisterPage';
import { meshAuthId, prefillFrom } from '../src/pages/nodes/MeshJoinPanel';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const ID = 'hskey-authreq-AbCdEfGhIjKlMnOpQrSt_-12';
const M = ko.fleet.meshJoin;

describe('meshAuthId', () => {
  it('takes the id out of the registration URL or the bare id', () => {
    expect(meshAuthId(`  https://hs.example.test/register/${ID}\n`)).toBe(ID);
    expect(meshAuthId(ID)).toBe(ID);
    expect(meshAuthId(`${ID} again ${ID}`)).toBe(ID);
  });

  it('refuses none, two different ids, or a longer string', () => {
    expect(meshAuthId('https://hs.example.test/register/nodekey:abc')).toBeNull();
    expect(meshAuthId(`${ID} ${ID.replace('Ab', 'Zz')}`)).toBeNull();
    expect(meshAuthId(`${ID}x`)).toBeNull();
  });
});

describe('prefillFrom', () => {
  it('fills the wizard with the tailnet IP and keeps only a valid node id', () => {
    const base = { ok: true, node_id: 3, ipv4: '100.64.0.30', ipv6: '', tags: ['tag:fleet'], user: 'owner', host: 'hs-1' };
    expect(prefillFrom({ ...base, name: 'edge-7' })).toEqual({ id: 'edge-7', hostname: '100.64.0.30', tags: ['tag:fleet'] });
    expect(prefillFrom({ ...base, name: 'Edge_7' }).id).toBe('');
  });
});

const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <NodeRegisterPage />
      </HubStoreProvider>
    </GuardianRoot>
  );

describe('mesh join panel', () => {
  beforeEach(() => {
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
    mocked.fetchNodeRegistration.mockResolvedValue({ fleet_file: '/home/ubuntu/.ssh/guardian_fleet.conf', unmanaged_hosts: [], blockers: [], can_register: true });
    mocked.joinMesh.mockReset();
  });

  it('says how to turn the feature on when the hub has it off', async () => {
    mocked.fetchMeshJoin.mockResolvedValue({ enabled: false });
    renderPage();
    expect(await screen.findByText(M.off)).toBeTruthy();
  });

  it('approves a login and fills the add-node wizard with the joined node', async () => {
    mocked.fetchMeshJoin.mockResolvedValue({
      enabled: true, ready: true, host: 'hs-1', user: 'owner', tags: ['tag:fleet', 'tag:edge'], server_url: 'https://hs.example.test',
    });
    mocked.joinMesh.mockResolvedValue({
      ok: true, node_id: 30, name: 'edge-7', ipv4: '100.64.0.30', ipv6: '', tags: ['tag:fleet'], user: 'owner', host: 'hs-1',
    });
    renderPage();
    fireEvent.change(await screen.findByLabelText(M.nameLabel), { target: { value: 'edge-7' } });
    expect(screen.getByText(/--login-server https:\/\/hs\.example\.test --hostname edge-7/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: M.approve }));
    expect(await screen.findByText(M.authInvalid)).toBeTruthy();
    expect(mocked.joinMesh).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(M.authLabel), { target: { value: `https://hs.example.test/register/${ID}` } });
    fireEvent.click(screen.getByRole('button', { name: M.approve }));
    expect(await screen.findByText(M.joined('edge-7', '100.64.0.30'))).toBeTruthy();
    expect(mocked.joinMesh).toHaveBeenCalledWith({ auth: ID, tags: ['tag:fleet'], name: 'edge-7' });

    fireEvent.click(screen.getByRole('button', { name: M.continueRegister }));
    await waitFor(() => expect((screen.getByLabelText(ko.fleet.forms.connection.hostName) as HTMLInputElement).value).toBe('100.64.0.30'));
    expect((screen.getByLabelText(ko.fleet.forms.connection.id) as HTMLInputElement).value).toBe('edge-7');
  });
});

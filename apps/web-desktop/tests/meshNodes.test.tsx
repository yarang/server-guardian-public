import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { MeshNodesPanel } from '../src/pages/nodes/MeshNodesPanel';
import type { MeshNodeList } from '../src/types';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const N = ko.fleet.meshNodes;

const list: MeshNodeList = {
  host: 'hs-1',
  tags: ['tag:fleet'],
  nodes: [
    { id: 1, name: 'hs-1', ipv4: '100.64.0.1', ipv6: '', tags: ['tag:mesh-ctl'], user: 'tagged-devices', online: true, last_seen: 0, protected: 'control_server' },
    { id: 7, name: 'hub-1', ipv4: '100.64.0.7', ipv6: '', tags: ['tag:fleet'], user: 'tagged-devices', online: true, last_seen: 0, inventory_id: 'hub-1', protected: 'hub' },
    { id: 12, name: 'edge-7', ipv4: '100.64.0.30', ipv6: '', tags: ['tag:fleet'], user: 'tagged-devices', online: false, last_seen: 1792200000, inventory_id: 'edge-7' },
  ],
};

const renderPanel = () =>
  render(
    <GuardianRoot>
      <MeshNodesPanel />
    </GuardianRoot>
  );

describe('tailnet node panel', () => {
  beforeEach(() => {
    mocked.fetchMeshNodes.mockResolvedValue(list);
    mocked.removeMeshNode.mockReset();
  });

  it('is hidden while the mesh feature is off', async () => {
    mocked.fetchMeshJoin.mockResolvedValue({ enabled: false });
    const { container } = renderPanel();
    await waitFor(() => expect(mocked.fetchMeshJoin).toHaveBeenCalled());
    expect(container.textContent).toBe('');
    expect(mocked.fetchMeshNodes).not.toHaveBeenCalled();
  });

  it('removes only after the name is typed, and warns about a Guardian node', async () => {
    mocked.fetchMeshJoin.mockResolvedValue({ enabled: true, ready: true, host: 'hs-1', user: 'owner', tags: ['tag:fleet'] });
    mocked.removeMeshNode.mockResolvedValue({ ok: true, id: 12, name: 'edge-7', ipv4: '100.64.0.30', inventory_id: 'edge-7' });
    renderPanel();
    expect(await screen.findByText(N.protectedReason.hub)).toBeTruthy();
    expect(screen.getByText(N.protectedReason.control_server)).toBeTruthy();
    const buttons = screen.getAllByRole('button', { name: N.remove });
    expect(buttons).toHaveLength(1);

    fireEvent.click(buttons[0]);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(N.inventoryWarn('edge-7'), { exact: false })).toBeTruthy();
    const confirm = within(dialog).getByRole('button', { name: N.confirm });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(N.confirmLabel), { target: { value: 'edge-7' } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);
    expect(await screen.findByText(N.removed('edge-7'))).toBeTruthy();
    expect(mocked.removeMeshNode).toHaveBeenCalledWith(12, 'edge-7');
    expect(mocked.fetchMeshNodes).toHaveBeenCalledTimes(2);
  });
});

import { tr } from '../../i18n';
import { useState } from 'react';
import { Button, Panel, StatusChip, TextField } from '@server-guardian/ui';
import { HubError, fetchMeshJoin, fetchMeshNodes, removeMeshNode } from '../../api';
import { Ago } from '../../components/Ago';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Notice } from '../../components/Notice';
import { Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import { fleetHref } from '../../router';
import type { MeshNode, MeshNodeList } from '../../types';

const T = () => tr.fleet.meshNodes;

function RemoveDialog({ node, onDone, onCancel }: { node: MeshNode; onDone: () => void; onCancel: () => void }) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await removeMeshNode(node.id, typed);
      onDone();
    } catch (err) {
      setError(err instanceof HubError && err.hint ? `${err.message}\n${err.hint}` : errorText(err));
      setBusy(false);
    }
  };
  return (
    <ConfirmModal
      title={T().confirmTitle(node.name)}
      message={
        <Stack gap="var(--sg-s-4)">
          <span>{T().confirmBody}</span>
          {node.inventory_id ? (
            <Notice tone="warn">
              {T().inventoryWarn(node.inventory_id)} <a href={fleetHref('nodes', [node.inventory_id])}>{node.inventory_id}</a>
            </Notice>
          ) : null}
          <TextField label={T().confirmLabel} mono value={typed} placeholder={node.name} onChange={(e) => setTyped(e.target.value)} />
        </Stack>
      }
      confirmLabel={T().confirm}
      confirmDisabled={typed !== node.name}
      busy={busy}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={onCancel}
    />
  );
}

function NodeTable({ list, onRemove }: { list: MeshNodeList; onRemove: (n: MeshNode) => void }) {
  const columns: Column<MeshNode>[] = [
    { header: T().cols.name, render: (n) => <span style={mono}>{n.name}</span> },
    { header: T().cols.ip, render: (n) => <span style={mono}>{n.ipv4 || '—'}</span> },
    { header: T().cols.tags, render: (n) => <span style={{ ...mono, fontSize: 12 }}>{n.tags.join(', ') || n.user || '—'}</span> },
    {
      header: T().cols.state,
      render: (n) =>
        n.online ? (
          <StatusChip tone="ok">{T().online}</StatusChip>
        ) : (
          <span>
            <StatusChip tone="warn">{T().offline}</StatusChip>{' '}
            {n.last_seen ? (
              <span style={{ ...muted, fontSize: 12 }}>
                <Ago at={n.last_seen * 1000} />
              </span>
            ) : null}
          </span>
        ),
    },
    {
      header: T().cols.guardian,
      render: (n) => (n.inventory_id ? <a href={fleetHref('nodes', [n.inventory_id])}>{n.inventory_id}</a> : '—'),
    },
    {
      header: T().cols.action,
      render: (n) =>
        n.protected ? (
          <span style={{ ...muted, fontSize: 12 }}>{T().protectedReason[n.protected]}</span>
        ) : (
          <Button size="sm" variant="outline" onClick={() => onRemove(n)}>
            {T().remove}
          </Button>
        ),
    },
  ];
  return <DataTable label={T().aria} columns={columns} rows={list.nodes} getKey={(n) => String(n.id)} />;
}

function Nodes() {
  const nodes = useHubData(fetchMeshNodes);
  const [removing, setRemoving] = useState<MeshNode | null>(null);
  const [removed, setRemoved] = useState<string | null>(null);
  return (
    <Stack gap="var(--sg-s-5)">
      <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{T().intro}</div>
      {removed ? <Notice tone="ok">{T().removed(removed)}</Notice> : null}
      <DataGate state={nodes} what={T().what}>
        {(list) => <NodeTable list={list} onRemove={setRemoving} />}
      </DataGate>
      {removing ? (
        <RemoveDialog
          node={removing}
          onCancel={() => setRemoving(null)}
          onDone={() => {
            setRemoved(removing.name);
            setRemoving(null);
            nodes.reload();
          }}
        />
      ) : null}
    </Stack>
  );
}

/** The tailnet's nodes with removal (docs/design/mesh-join.md, section 8); hidden while the mesh feature is off. */
export function MeshNodesPanel() {
  const info = useHubData(fetchMeshJoin);
  if (!info.data?.enabled) return null;
  return (
    <Panel title={T().title}>
      <Nodes />
    </Panel>
  );
}

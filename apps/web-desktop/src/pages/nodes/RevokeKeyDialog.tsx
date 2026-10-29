import { tr } from '../../i18n';
import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { HubError, fetchRevokeKeyPreview, revokeNodeKey } from '../../api';
import { DataGate } from '../../components/DataGate';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import type { RevokeKeyResult, FleetNode } from '../../types';
import { toFormError } from './NodeForms';

function Fact({ label, children }: { label: string; children: string }) {
  return (
    <>
      <span style={muted}>{label}</span>
      <span style={{ ...mono, overflowWrap: 'anywhere' }}>{children}</span>
    </>
  );
}

/**
 * Revokes the hub's SSH key on a node (GET previews, POST removes it from the node's authorized_keys) after the
 * operator types the node id. The hub loses access to the machine, so the next step is Remove node.
 */
export function RevokeKeyDialog({ node, onClose }: { node: FleetNode; onClose: () => void }) {
  const preview = useHubData(() => fetchRevokeKeyPreview(node.id));
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RevokeKeyResult | null>(null);

  const revoke = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await revokeNodeKey(node.id));
    } catch (err) {
      const fe = toFormError(err);
      if (fe) {
        const reach = err instanceof HubError && err.reachability ? tr.fleet.revokeKey.reachability(err.reachability) : '';
        setError(`${fe.message}${reach}`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={tr.fleet.revokeKey.title(node.name)} width={600} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        {result ? (
          <>
            <Notice tone="ok">{tr.fleet.revokeKey.removed(result.removed, node.id, result.note)}</Notice>
            <div style={{ lineHeight: 1.6 }}>
              {tr.fleet.revokeKey.afterPre}
              <b>{tr.fleet.removeNode.remove}</b>
              {tr.fleet.revokeKey.afterPost}
            </div>
            <Row style={{ justifyContent: 'flex-end' }}>
              <Button variant="primary" onClick={onClose}>
                {tr.fleet.revokeKey.done}
              </Button>
            </Row>
          </>
        ) : (
          <>
            <DataGate state={preview} what={tr.fleet.revokeKey.previewWhat}>
              {(p) => (
                <Stack gap="var(--sg-s-4)">
                  <div style={{ display: 'grid', gridTemplateColumns: 'max-content 1fr', columnGap: 'var(--sg-s-5)', rowGap: 'var(--sg-s-2)', fontSize: 13 }}>
                    <Fact label={tr.fleet.revokeKey.target}>{p.target}</Fact>
                    <Fact label={tr.fleet.revokeKey.fingerprint}>{p.fingerprint}</Fact>
                    <Fact label={tr.fleet.revokeKey.keyMatch}>{p.match}</Fact>
                  </div>
                  {p.note ? <div style={{ ...muted, fontSize: 13 }}>{p.note}</div> : null}
                </Stack>
              )}
            </DataGate>
            <Notice tone="warn">
              {tr.fleet.revokeKey.warning}
            </Notice>
            <TextField
              label={tr.fleet.removeNode.typeToConfirm(node.id)}
              mono
              value={typed}
              autoComplete="off"
              disabled={busy}
              onChange={(e) => setTyped(e.target.value)}
            />
            {error ? <Notice tone="crit">{error}</Notice> : null}
            <Row style={{ justifyContent: 'flex-end' }}>
              <Button variant="ghost" disabled={busy} onClick={onClose}>
                {tr.common.cancel}
              </Button>
              <Button variant="danger" loading={busy} disabled={typed !== node.id || preview.data === null} onClick={() => void revoke()}>
                {tr.fleet.revokeKey.revoke}
              </Button>
            </Row>
          </>
        )}
      </Stack>
    </Modal>
  );
}

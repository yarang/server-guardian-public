import { tr } from '../../i18n';
import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { deleteNode } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { CheckboxField } from '../../components/CheckboxField';
import { Row, Stack, mono } from '../../components/layout';
import { fmtTime } from '../../lib/debate';
import type { NodeRemoveResult, FleetNode } from '../../types';
import { toFormError } from './NodeForms';

/**
 * Removes a node from the hub (DELETE /api/nodes/{id}) after the operator types its id. The hub
 * deletes the Host block, the pinned host key and the inventory entry; the machine is not touched.
 * Revoking the hub key comes first: once the node is removed the dashboard can no longer do it, so an
 * unrevoked node needs an explicit "Remove without revoking" tick before the typed-id confirm works.
 */
export function RemoveNodeDialog({
  node,
  onClose,
  onRemoved,
  onRevokeFirst,
}: {
  node: FleetNode;
  onClose: () => void;
  /** Opens the revoke-key dialog (the caller closes this one). */
  onRevokeFirst: () => void;
  onRemoved: (result: NodeRemoveResult) => void;
}) {
  const [typed, setTyped] = useState('');
  const [skipRevoke, setSkipRevoke] = useState(false);
  const revokedAt = node.hub_key_revoked_at ?? null;
  const needsRevoke = node.guardian_role !== 'hub' && !revokedAt;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      onRemoved(await deleteNode(node.id));
    } catch (err) {
      const fe = toFormError(err);
      if (fe) setError(`${fe.message}${fe.hint ? `\n${fe.hint}` : ''}`);
      setBusy(false);
    }
  };

  return (
    <Modal title={tr.fleet.removeNode.title(node.name)} width={560} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        <div style={{ lineHeight: 1.6, overflowWrap: 'anywhere' }}>
          {tr.fleet.removeNode.bodyPre}
          <b style={mono}>{node.id}</b>
          {tr.fleet.removeNode.bodyPost}
        </div>
        {revokedAt ? (
          <Notice tone="ok">{tr.fleet.removeNode.revoked(fmtTime(revokedAt))}</Notice>
        ) : needsRevoke ? (
          <Notice tone="warn">
            <Stack gap="var(--sg-s-4)">
              <div>
                {tr.fleet.removeNode.needsRevoke}
              </div>
              <Row>
                <Button size="sm" variant="primary" disabled={busy} onClick={onRevokeFirst}>
                  {tr.fleet.removeNode.revokeFirst}
                </Button>
              </Row>
              <CheckboxField checked={skipRevoke} disabled={busy} onChange={(e) => setSkipRevoke(e.target.checked)}>
                {tr.fleet.removeNode.skipRevoke}
              </CheckboxField>
            </Stack>
          </Notice>
        ) : null}
        <Notice tone="warn">
          {tr.fleet.removeNode.groupsWarning(node.id)}
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
          <Button variant="danger" loading={busy} disabled={typed !== node.id || (needsRevoke && !skipRevoke)} onClick={() => void remove()}>
            {tr.fleet.removeNode.remove}
          </Button>
        </Row>
      </Stack>
    </Modal>
  );
}

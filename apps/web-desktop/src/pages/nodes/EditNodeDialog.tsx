import { tr } from '../../i18n';
import { useEffect, useState } from 'react';
import { Button } from '@server-guardian/ui';
import { fetchNodeRegistration, updateNode } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice, NoticeList } from '../../components/Notice';
import { Row, Stack, muted } from '../../components/layout';
import {
  connectionChanged,
  connectionOf,
  destinationChanged,
  metadataOf,
  pinTarget,
  updatePayload,
  validateConnection,
  validateMetadata,
} from '../../lib/nodeForm';
import type { ConnectionErrors, ConnectionForm, MetadataErrors, MetadataForm } from '../../lib/nodeForm';
import type { GroupCatalogField, NodeWriteResult, FleetNode } from '../../types';
import {
  ConnectionFields,
  HostKeyPanel,
  MetadataFields,
  ProbeOutcome,
  confirmedFingerprint,
  toFormError,
  useHostKeyCheck,
  useNodeProbe,
} from './NodeForms';

/**
 * Edit a node's connection and metadata (PUT /api/nodes/{id}). Changing the hostname or port needs a
 * fresh host key check and confirmation, as does a first connection edit of a node that is not in
 * the fleet file yet. The hub's probe result is shown after saving.
 */
export function EditNodeDialog({
  node,
  fields,
  onClose,
  onSaved,
}: {
  node: FleetNode;
  fields: GroupCatalogField[];
  onClose: () => void;
  /** The inventory changed: reload shared data. */
  onSaved: () => void;
}) {
  const [base] = useState(() => ({ conn: connectionOf(node), meta: metadataOf(node) }));
  const [conn, setConn] = useState<ConnectionForm>(base.conn);
  const [meta, setMeta] = useState<MetadataForm>(base.meta);
  const [connServer, setConnServer] = useState<ConnectionErrors>({});
  const [metaServer, setMetaServer] = useState<MetadataErrors>({});
  const [fingerprintError, setFingerprintError] = useState<string | undefined>(undefined);
  const [banner, setBanner] = useState<string | null>(null);
  /** Hand-written fleet block: the hub refuses connection edits (metadata still works). */
  const [locked, setLocked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<NodeWriteResult | null>(null);
  const check = useHostKeyCheck();
  const probe = useNodeProbe();

  // Learn up front whether the node's block is hand-written, so its connection fields are disabled before a 409.
  useEffect(() => {
    let current = true;
    fetchNodeRegistration()
      .then((r) => {
        if (current && r.unmanaged_hosts?.includes(node.id)) setLocked(true);
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [node.id]);

  const trimmed: ConnectionForm = { ...conn, hostname: conn.hostname.trim(), user: conn.user.trim(), port: conn.port.trim() };
  // An inventory node is reached over its Tailscale IP when it has one; the hub rescans that address, so scan it here too.
  const checkConn: ConnectionForm =
    node.connection === 'inventory' && trimmed.hostname === base.conn.hostname ? { ...trimmed, hostname: pinTarget(node).hostname } : trimmed;
  const connErrors = validateConnection(trimmed, true);
  const metaErrors = validateMetadata(meta);
  const needsCheck = !locked && (destinationChanged(base.conn, trimmed) || (node.connection === 'inventory' && connectionChanged(base.conn, trimmed)));
  const fingerprint = needsCheck ? confirmedFingerprint(check, checkConn) : null;
  const payload = updatePayload(base.conn, locked ? base.conn : trimmed, base.meta, meta, fingerprint);
  const valid = Object.keys(connErrors).length === 0 && Object.keys(metaErrors).length === 0;
  const canSave = valid && payload !== null && (!needsCheck || fingerprint !== null) && !saving;

  const editConn = (next: ConnectionForm) => {
    setConnServer((prev) => {
      const out = { ...prev };
      for (const k of Object.keys(next) as Array<keyof ConnectionForm>) if (next[k] !== conn[k]) delete out[k];
      return out;
    });
    setConn(next);
  };

  const runCheck = async () => {
    setBanner(null);
    const err = await check.run(checkConn);
    if (err?.field === 'hostname' || err?.field === 'user' || err?.field === 'port') {
      setConnServer((prev) => ({ ...prev, [err.field as keyof ConnectionForm]: err.message }));
    }
  };

  const save = async () => {
    if (!payload) return;
    setSaving(true);
    setBanner(null);
    setFingerprintError(undefined);
    try {
      const res = await updateNode(node.id, payload);
      setSaved(res);
      probe.show(res.probe, res.authorized_key, res.authorized_key_blocker);
      onSaved();
    } catch (err) {
      const fe = toFormError(err);
      if (!fe) return;
      if (fe.code === 'unmanaged_block') {
        setLocked(true);
        setConn(base.conn);
        check.clear();
        setBanner(tr.fleet.editNode.lockedBanner(fe.message));
      } else if (fe.code === 'fingerprint_mismatch') {
        check.adopt(checkConn, fe.keys ?? []);
        setBanner(fe.message);
      } else if (fe.field?.startsWith('metadata.')) {
        setMetaServer((prev) => ({ ...prev, [(fe.field as string).slice('metadata.'.length)]: fe.message }));
      } else if (fe.field === 'hostname' || fe.field === 'user' || fe.field === 'port') {
        setConnServer((prev) => ({ ...prev, [fe.field as keyof ConnectionForm]: fe.message }));
      } else if (fe.field === 'host_key_fingerprint') {
        check.setConfirmed(false);
        setFingerprintError(fe.message);
      } else {
        setBanner(`${fe.message}${fe.hint ? `\n${fe.hint}` : ''}`);
      }
    } finally {
      setSaving(false);
    }
  };

  const title = tr.fleet.editNode.title(node.name);
  if (saved) {
    return (
      <Modal title={title} width={640} onClose={onClose}>
        <Stack gap="var(--sg-s-6)">
          <Notice tone="ok">{tr.fleet.editNode.saved(node.id)}</Notice>
          <NoticeList tone="warn" items={saved.warnings ?? []} />
          {probe.probe ? (
            <ProbeOutcome
              nodeId={node.id}
              probe={probe.probe}
              authorizedKey={probe.authorizedKey}
              keyBlocker={probe.keyBlocker}
              testing={probe.testing}
              testError={probe.error}
              onTestAgain={() => void probe.test(node.id, onSaved)}
            />
          ) : (
            <div style={{ ...muted, fontSize: 13 }}>{tr.fleet.editNode.onlyMetadata}</div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button variant="primary" onClick={onClose}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Modal>
    );
  }

  return (
    <Modal title={title} width={640} onClose={saving ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        {locked ? (
          <Notice tone="warn">
            {tr.fleet.editNode.locked}
          </Notice>
        ) : node.connection === 'inventory' ? (
          <Notice tone="info">
            {tr.fleet.editNode.inventory}
          </Notice>
        ) : null}
        {banner ? <Notice tone="crit">{banner}</Notice> : null}
        <ConnectionFields value={conn} onChange={editConn} errors={{ ...connErrors, ...connServer }} idLocked disabled={locked} />
        {needsCheck ? (
          <Stack gap="var(--sg-s-3)">
            <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>
              {tr.fleet.editNode.destinationChanged}
            </div>
            <HostKeyPanel
              check={check}
              conn={checkConn}
              canCheck={Object.keys(connErrors).length === 0}
              onCheck={() => void runCheck()}
              fieldError={fingerprintError}
            />
          </Stack>
        ) : null}
        <MetadataFields value={meta} onChange={setMeta} errors={{ ...metaErrors, ...metaServer }} fields={fields} />
        <Row style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" disabled={saving} onClick={onClose}>
            {tr.common.cancel}
          </Button>
          <Button variant="primary" loading={saving} disabled={!canSave} onClick={() => void save()}>
            {tr.fleet.editNode.saveChanges}
          </Button>
        </Row>
      </Stack>
    </Modal>
  );
}

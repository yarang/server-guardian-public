import { tr } from '../../i18n';
import { useEffect, useState } from 'react';
import { Button } from '@server-guardian/ui';
import { fetchNodeRegistration, updateNode } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { validateHostname, pinTarget } from '../../lib/nodeForm';
import type { NodeWriteResult, FleetNode } from '../../types';
import { HostKeyPanel, ProbeOutcome, confirmedFingerprint, toFormError, useHostKeyCheck, useNodeProbe } from './NodeForms';

/**
 * Pins (or, for an already pinned node, re-confirms) a node's host key: checks the node's current
 * address, asks the operator to confirm a fingerprint, then PUTs only `host_key_fingerprint`. The
 * hub rewrites the node's block in the pinned shape and probes it.
 */
export function PinHostKeyDialog({ node, onClose, onSaved }: { node: FleetNode; onClose: () => void; onSaved: () => void }) {
  const target = pinTarget(node);
  const addressProblem = validateHostname(target.hostname);
  const rotating = node.host_key_mode === 'pinned';
  const [locked, setLocked] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [fingerprintError, setFingerprintError] = useState<string | undefined>(undefined);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<NodeWriteResult | null>(null);
  const check = useHostKeyCheck();
  const probe = useNodeProbe();
  const fingerprint = confirmedFingerprint(check, target);

  const noAddress = tr.fleet.pinKey.noAddress(node.name);

  const runCheck = async () => {
    setBanner(null);
    if (addressProblem) {
      setBanner(noAddress);
      return;
    }
    const err = await check.run(target);
    if (err?.field === 'hostname') setBanner(noAddress);
  };

  // Scan on open; the operator can scan again from the panel.
  useEffect(() => {
    void runCheck();
    fetchNodeRegistration()
      .then((r) => {
        if (r.unmanaged_hosts?.includes(node.id)) {
          setLocked(tr.fleet.pinKey.locked);
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    if (fingerprint === null) return;
    setSaving(true);
    setBanner(null);
    setFingerprintError(undefined);
    try {
      const res = await updateNode(node.id, { host_key_fingerprint: fingerprint });
      setSaved(res);
      probe.show(res.probe, res.authorized_key, res.authorized_key_blocker);
      onSaved();
    } catch (err) {
      const fe = toFormError(err);
      if (!fe) return;
      if (fe.code === 'unmanaged_block') setLocked(fe.message);
      else if (fe.code === 'fingerprint_mismatch') {
        check.adopt(target, fe.keys ?? []);
        setBanner(fe.message);
      } else if (fe.field === 'hostname') setBanner(`${noAddress}\n${fe.message}`);
      else if (fe.field === 'host_key_fingerprint') {
        check.setConfirmed(false);
        setFingerprintError(fe.message);
      } else setBanner(`${fe.message}${fe.hint ? `\n${fe.hint}` : ''}`);
    } finally {
      setSaving(false);
    }
  };

  const title = tr.fleet.pinKey.title(rotating, node.name);
  if (saved) {
    return (
      <Modal title={title} width={600} onClose={onClose}>
        <Stack gap="var(--sg-s-6)">
          <Notice tone="ok">{tr.fleet.pinKey.saved(node.id, saved.node.hostname, saved.node.port)}</Notice>
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
            <div style={{ ...muted, fontSize: 13 }}>{tr.fleet.pinKey.noProbe}</div>
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
    <Modal title={title} width={600} onClose={saving ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        <div style={{ lineHeight: 1.6, overflowWrap: 'anywhere' }}>
          {tr.fleet.pinKey.introPre}
          <b style={mono}>{`${target.hostname}:${target.port}`}</b>
          {tr.fleet.pinKey.introPost(rotating)}
        </div>
        {locked ? <Notice tone="warn">{locked}</Notice> : null}
        {banner ? <Notice tone="crit">{banner}</Notice> : null}
        <HostKeyPanel check={check} conn={target} canCheck={!locked && !addressProblem} onCheck={() => void runCheck()} checkLabel={tr.fleet.forms.hostKey.checkAgain} fieldError={fingerprintError} />
        <Row style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" disabled={saving} onClick={onClose}>
            {tr.common.cancel}
          </Button>
          <Button variant="primary" loading={saving} disabled={fingerprint === null || locked !== null} onClick={() => void save()}>
            {rotating ? tr.fleet.pinKey.replacePinned : tr.fleet.pinKey.pin}
          </Button>
        </Row>
      </Stack>
    </Modal>
  );
}

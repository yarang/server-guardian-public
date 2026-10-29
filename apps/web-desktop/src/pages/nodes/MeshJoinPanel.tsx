import { tr } from '../../i18n';
import { useState } from 'react';
import { Button, CodeBlock, Panel, TextField } from '@server-guardian/ui';
import { HubError, fetchMeshJoin, joinMesh } from '../../api';
import { Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { TextAreaField } from '../../components/TextAreaField';
import { Row, Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import type { MeshJoinInfo, MeshJoinResult } from '../../types';

const AUTH_ID = /hskey-authreq-[A-Za-z0-9_-]{24}(?![A-Za-z0-9_-])/g;
const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** The one registration id in what the operator pasted, or null (none, several, or too long). */
export function meshAuthId(text: string): string | null {
  const ids = new Set(text.match(AUTH_ID) ?? []);
  return ids.size === 1 ? [...ids][0] : null;
}

/** What the add-node wizard can take over from a joined node. */
export interface MeshPrefill {
  id: string;
  hostname: string;
  tags: string[];
}

export function prefillFrom(res: MeshJoinResult): MeshPrefill {
  return { id: NAME.test(res.name) ? res.name : '', hostname: res.ipv4, tags: res.tags };
}

function JoinForm({ info, onPrefill }: { info: MeshJoinInfo; onPrefill: (p: MeshPrefill) => void }) {
  const tags = info.tags ?? [];
  const [auth, setAuth] = useState('');
  const [chosen, setChosen] = useState<string[]>(tags.slice(0, 1));
  const [name, setName] = useState('');
  const [attempted, setAttempted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<MeshJoinResult | null>(null);

  const authId = meshAuthId(auth);
  const nameError = name.trim() !== '' && !NAME.test(name.trim()) ? tr.fleet.forms.connection.idHint : undefined;
  const authError = (attempted || auth.trim() !== '') && authId === null ? tr.fleet.meshJoin.authInvalid : undefined;
  const tagError = attempted && chosen.length === 0 ? tr.fleet.meshJoin.tagsRequired : undefined;
  const command = `sudo tailscale up --login-server ${info.server_url ?? '<login server>'} --hostname ${name.trim() || '<name>'}`;

  const approve = async () => {
    setAttempted(true);
    setError(null);
    if (authId === null || chosen.length === 0 || nameError) return;
    setBusy(true);
    try {
      setDone(await joinMesh({ auth: authId, tags: chosen, name: name.trim() || undefined }));
    } catch (err) {
      setError(err instanceof HubError && err.hint ? `${err.message}\n${err.hint}` : errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <Stack gap="var(--sg-s-5)">
        <Notice tone="ok">{tr.fleet.meshJoin.joined(done.name, done.ipv4)}</Notice>
        {done.warning ? <Notice tone="warn">{done.warning}</Notice> : null}
        <div style={{ ...muted, fontSize: 13 }}>{tr.fleet.meshJoin.continueHint}</div>
        <Row>
          <Button variant="primary" onClick={() => onPrefill(prefillFrom(done))}>
            {tr.fleet.meshJoin.continueRegister}
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              setDone(null);
              setAuth('');
              setName('');
              setAttempted(false);
            }}
          >
            {tr.fleet.meshJoin.another}
          </Button>
        </Row>
      </Stack>
    );
  }

  return (
    <Stack gap="var(--sg-s-5)">
      <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{tr.fleet.meshJoin.intro}</div>
      <div style={{ ...muted, fontSize: 12 }}>{tr.fleet.meshJoin.controlServer(info.host ?? '', info.user ?? '')}</div>
      <TextField
        label={tr.fleet.meshJoin.nameLabel}
        mono
        value={name}
        hint={tr.fleet.meshJoin.nameHint}
        error={nameError}
        onChange={(e) => setName(e.target.value)}
      />
      <Stack gap="var(--sg-s-2)">
        <Caption>{tr.fleet.meshJoin.runOnNode}</Caption>
        <CodeBlock code={command} />
      </Stack>
      <TextAreaField
        label={tr.fleet.meshJoin.authLabel}
        mono
        rows={2}
        value={auth}
        hint={tr.fleet.meshJoin.authHint}
        error={authError}
        onChange={(e) => setAuth(e.target.value)}
      />
      <fieldset style={{ border: 0, margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-2)' }}>
        <legend style={{ color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600, padding: 0 }}>
          {tr.fleet.meshJoin.tagsLabel}
        </legend>
        {tags.map((t) => (
          <CheckboxField
            key={t}
            checked={chosen.includes(t)}
            onChange={(e) => setChosen(e.target.checked ? [...chosen, t] : chosen.filter((c) => c !== t))}
          >
            {t}
          </CheckboxField>
        ))}
        {tagError ? (
          <span role="alert" style={{ color: 'var(--sg-crit)', fontSize: 12 }}>
            {tagError}
          </span>
        ) : null}
      </fieldset>
      {error ? <Notice tone="crit">{error}</Notice> : null}
      <Row>
        <Button variant="primary" loading={busy} onClick={() => void approve()}>
          {tr.fleet.meshJoin.approve}
        </Button>
      </Row>
    </Stack>
  );
}

/**
 * Approves a new server's headscale login from the dashboard (docs/design/mesh-join.md). Shown above the add-node
 * wizard; a joined node can fill the wizard with its name and tailnet IP.
 */
export function MeshJoinPanel({ onPrefill }: { onPrefill: (p: MeshPrefill) => void }) {
  const info = useHubData(fetchMeshJoin);
  return (
    <Panel title={tr.fleet.meshJoin.title}>
      <DataGate state={info} what={tr.fleet.meshJoin.what}>
        {(i) =>
          !i.enabled ? (
            <Notice tone="info">{i.problem ? tr.fleet.meshJoin.notReady(i.problem) : tr.fleet.meshJoin.off}</Notice>
          ) : !i.ready ? (
            <Notice tone="warn">{tr.fleet.meshJoin.notReady(i.problem ?? '')}</Notice>
          ) : (
            <JoinForm info={i} onPrefill={onPrefill} />
          )
        }
      </DataGate>
    </Panel>
  );
}

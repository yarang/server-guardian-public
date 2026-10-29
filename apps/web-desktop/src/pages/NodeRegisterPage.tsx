import { tr } from '../i18n';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { Button, CodeBlock, Panel } from '@server-guardian/ui';
import { fetchNodeRegistration, registerNode } from '../api';
import { Caption } from '../components/Card';
import { DataGate } from '../components/DataGate';
import { Notice, NoticeList } from '../components/Notice';
import { Row, Stack, mono, muted } from '../components/layout';
import { useHubData } from '../hooks/useHubData';
import {
  DEFAULT_PORT,
  DEFAULT_USER,
  EMPTY_METADATA,
  metadataPayload,
  validateConnection,
  validateMetadata,
} from '../lib/nodeForm';
import type { ConnectionErrors, ConnectionForm, MetadataErrors, MetadataForm } from '../lib/nodeForm';
import { fleetHref } from '../router';
import { useHubStore } from '../state/hubStore';
import type { NodeRegistration, NodeWriteResult } from '../types';
import {
  ConnectionFields,
  HostKeyPanel,
  MetadataFields,
  ProbeOutcome,
  confirmedFingerprint,
  toFormError,
  useHostKeyCheck,
  useNodeProbe,
} from './nodes/NodeForms';
import { MeshJoinPanel } from './nodes/MeshJoinPanel';
import type { MeshPrefill } from './nodes/MeshJoinPanel';

type Step = 1 | 2 | 3;

const STEPS: Array<{ n: Step; label: string }> = [
  { n: 1, label: tr.fleet.register.steps.connection },
  { n: 2, label: tr.fleet.register.steps.metadata },
  { n: 3, label: tr.fleet.register.steps.register },
];

const FIELD_STEP_PREFIX = 'metadata.';

function Steps({ current }: { current: Step | 'done' }) {
  return (
    <ol style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', margin: 0, padding: 0, listStyle: 'none' }}>
      {STEPS.map((s) => {
        const active = current === s.n;
        return (
          <li
            key={s.n}
            aria-current={active ? 'step' : undefined}
            style={{ fontSize: 13, fontWeight: active ? 700 : 500, color: active ? 'var(--sg-text)' : 'var(--sg-text-muted)' }}
          >
            {`${s.n}. ${s.label}`}
          </li>
        );
      })}
    </ol>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-1)', minWidth: 0 }}>
      <span style={{ ...muted, fontSize: 12, fontWeight: 600 }}>{label}</span>
      <span style={{ ...mono, overflowWrap: 'anywhere' }}>{children}</span>
    </div>
  );
}

/** Registration is unavailable: what the hub says is missing, with the line to add and where the fleet file lives. */
function Blocked({ reg }: { reg: NodeRegistration }) {
  return (
    <Panel title={tr.fleet.register.addNode}>
      <Stack gap="var(--sg-s-6)">
        <Notice tone="warn">{tr.fleet.register.blockedNotice}</Notice>
        <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', lineHeight: 1.6 }}>
          {reg.blockers.map((b) => (
            <li key={b} style={{ overflowWrap: 'anywhere' }}>
              {b}
            </li>
          ))}
        </ul>
        {reg.include_effective === false && reg.include_line ? (
          <Stack gap="var(--sg-s-3)">
            <Caption>{tr.fleet.register.includeCaption(reg.ssh_config_file ?? tr.fleet.register.defaultSshConfig)}</Caption>
            <CodeBlock code={reg.include_line} />
          </Stack>
        ) : null}
        {reg.inventory_writable === false && reg.inventory_file ? (
          <div style={{ ...muted, fontSize: 13 }}>
            {tr.fleet.register.inventoryFile}
            <span style={mono}>{reg.inventory_file}</span>
          </div>
        ) : null}
        <div style={{ ...muted, fontSize: 13 }}>
          {tr.fleet.register.fleetFile}
          <span style={mono}>{reg.fleet_file}</span>
        </div>
        <div>
          <a href={fleetHref('nodes')}>{tr.fleet.register.backToNodes}</a>
        </div>
      </Stack>
    </Panel>
  );
}

/** Keeps only the entries of `errors` whose field has text or after the first attempt, so a blank form is not red. */
function visibleErrors<T extends Record<string, string | undefined>>(errors: T, values: Record<string, string>, attempted: boolean): T {
  const out: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(errors)) if (v && (attempted || (values[k] ?? '') !== '')) out[k] = v;
  return out as T;
}

function Wizard({ reg, onAnother, prefill }: { reg: NodeRegistration; onAnother: () => void; prefill?: MeshPrefill }) {
  const { servers, groups } = useHubStore();
  const [step, setStep] = useState<Step>(1);
  const [conn, setConnState] = useState<ConnectionForm>({
    id: prefill?.id ?? '',
    hostname: prefill?.hostname ?? '',
    user: DEFAULT_USER,
    port: DEFAULT_PORT,
  });
  const [meta, setMetaState] = useState<MetadataForm>(prefill ? { ...EMPTY_METADATA, tailscale_tags: prefill.tags } : EMPTY_METADATA);
  const [attempted, setAttempted] = useState(false);
  const [connServer, setConnServer] = useState<ConnectionErrors>({});
  const [metaServer, setMetaServer] = useState<MetadataErrors>({});
  const [banner, setBanner] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);
  const [done, setDone] = useState<NodeWriteResult | null>(null);
  const check = useHostKeyCheck();
  const probe = useNodeProbe();

  const existing = servers.data?.some((s) => s.id === conn.id.trim()) ?? false;
  const connClient: ConnectionErrors = validateConnection({ ...conn, id: conn.id.trim() });
  if (!connClient.id && existing) connClient.id = tr.fleet.register.idExists;
  const connShown = { ...visibleErrors(connClient, { ...conn }, attempted), ...connServer };
  const metaClient = validateMetadata(meta);
  const metaShown: MetadataErrors = { ...metaClient, ...metaServer };

  const setConn = (next: ConnectionForm) => {
    // Editing a field drops the hub's complaint about it.
    setConnServer((prev) => {
      const out = { ...prev };
      for (const k of Object.keys(next) as Array<keyof ConnectionForm>) if (next[k] !== conn[k]) delete out[k];
      return out;
    });
    setConnState(next);
  };
  const setMeta = (next: MetadataForm) => {
    setMetaServer((prev) => {
      const out = { ...prev };
      for (const k of Object.keys(next) as Array<keyof MetadataForm>) if (next[k] !== meta[k]) delete out[k];
      return out;
    });
    setMetaState(next);
  };

  const connValid = Object.keys(connClient).length === 0;
  const fingerprint = confirmedFingerprint(check, { ...conn, id: conn.id.trim() });
  const idTaken = check.result?.id_exists === true && check.sig !== null;
  const hubFieldError = connServer as Record<string, string | undefined>;

  const runCheck = async () => {
    setAttempted(true);
    setBanner(null);
    if (!connValid) return;
    const err = await check.run(conn);
    if (err?.field && ['id', 'hostname', 'user', 'port'].includes(err.field)) {
      setConnServer((prev) => ({ ...prev, [err.field as keyof ConnectionForm]: err.message }));
    }
  };

  const nextFromConnection = () => {
    setAttempted(true);
    if (!connValid || fingerprint === null) return;
    setBanner(null);
    setStep(2);
  };

  const register = async () => {
    if (fingerprint === null) return;
    setRegistering(true);
    setBanner(null);
    try {
      const res = await registerNode({
        id: conn.id.trim(),
        hostname: conn.hostname.trim(),
        user: conn.user.trim(),
        port: Number(conn.port.trim()),
        host_key_fingerprint: fingerprint,
        metadata: metadataPayload(meta),
      });
      setDone(res);
      probe.show(res.probe, res.authorized_key, res.authorized_key_blocker);
      servers.reload();
      groups.reload();
    } catch (err) {
      const fe = toFormError(err);
      if (!fe) return;
      if (fe.code === 'fingerprint_mismatch') {
        // The node presents other keys than the one confirmed: show them and ask again.
        check.adopt(conn, fe.keys ?? []);
        setBanner(fe.message);
        setStep(1);
      } else if (fe.field?.startsWith(FIELD_STEP_PREFIX)) {
        setMetaServer((prev) => ({ ...prev, [(fe.field as string).slice(FIELD_STEP_PREFIX.length)]: fe.message }));
        setBanner(tr.fleet.register.rejectedMetadata(fe.message));
        setStep(2);
      } else if (fe.field === 'id' || fe.field === 'hostname' || fe.field === 'user' || fe.field === 'port') {
        setConnServer((prev) => ({ ...prev, [fe.field as keyof ConnectionForm]: fe.message }));
        setBanner(tr.fleet.register.rejectedConnection(fe.message));
        setStep(1);
      } else if (fe.field === 'host_key_fingerprint') {
        check.setConfirmed(false);
        setBanner(fe.message);
        setStep(1);
      } else {
        setBanner(`${fe.message}${fe.hint ? `\n${fe.hint}` : ''}`);
      }
    } finally {
      setRegistering(false);
    }
  };

  if (done) {
    return (
      <Panel title={tr.fleet.register.registeredTitle}>
        <Stack gap="var(--sg-s-6)">
          <Steps current="done" />
          <Notice tone="ok">{tr.fleet.register.registeredNotice(done.node.id, done.node.hostname, done.node.port)}</Notice>
          <NoticeList tone="warn" items={done.warnings ?? []} />
          {probe.probe ? (
            <ProbeOutcome
              nodeId={done.node.id}
              probe={probe.probe}
              authorizedKey={probe.authorizedKey}
              keyBlocker={probe.keyBlocker}
              testing={probe.testing}
              testError={probe.error}
              onTestAgain={() => void probe.test(done.node.id, servers.reload)}
            />
          ) : (
            <Notice tone="warn">{tr.fleet.register.noProbe}</Notice>
          )}
          <Row>
            <Button variant="primary" href={fleetHref('nodes', [done.node.id])}>
              {tr.fleet.register.openNodePage}
            </Button>
            <Button variant="outline" onClick={onAnother}>
              {tr.fleet.register.addAnother}
            </Button>
            <a href={fleetHref('nodes')}>{tr.fleet.register.backToNodes}</a>
          </Row>
        </Stack>
      </Panel>
    );
  }

  return (
    <Panel title={tr.fleet.register.addNode}>
      <Stack gap="var(--sg-s-6)">
        <Steps current={step} />
        <div style={{ ...muted, fontSize: 12 }}>
          {tr.fleet.register.fleetFile}
          <span style={mono}>{reg.fleet_file}</span>
        </div>
        {reg.fleet_public_key_note ? <Notice tone="warn">{reg.fleet_public_key_note}</Notice> : null}
        {reg.fleet_public_key_blocker ? <Notice tone="warn">{reg.fleet_public_key_blocker}</Notice> : null}
        {banner ? <Notice tone="crit">{banner}</Notice> : null}

        {step === 1 ? (
          <>
            <ConnectionFields value={conn} onChange={setConn} errors={connShown} />
            <HostKeyPanel
              check={check}
              conn={{ ...conn, id: conn.id.trim() }}
              canCheck={connValid}
              onCheck={() => void runCheck()}
              idMustBeNew
              fieldError={hubFieldError.host_key_fingerprint}
            />
            <Row>
              <Button variant="primary" disabled={!connValid || fingerprint === null || idTaken} onClick={nextFromConnection}>
                {tr.fleet.register.nextMetadata}
              </Button>
              <a href={fleetHref('nodes')}>{tr.common.cancel}</a>
            </Row>
          </>
        ) : null}

        {step === 2 ? (
          <>
            <MetadataFields value={meta} onChange={setMeta} errors={metaShown} fields={groups.data?.catalog?.fields ?? []} />
            <Row>
              <Button variant="ghost" onClick={() => setStep(1)}>
                {tr.common.back}
              </Button>
              <Button variant="primary" disabled={Object.keys(metaClient).length > 0} onClick={() => setStep(3)}>
                {tr.fleet.register.nextReview}
              </Button>
            </Row>
          </>
        ) : null}

        {step === 3 ? (
          <>
            <Row gap="var(--sg-s-8)">
              <Fact label={tr.fleet.register.facts.id}>{conn.id.trim()}</Fact>
              <Fact label={tr.fleet.register.facts.hostName}>{conn.hostname.trim()}</Fact>
              <Fact label={tr.fleet.register.facts.user}>{conn.user.trim()}</Fact>
              <Fact label={tr.fleet.register.facts.port}>{conn.port.trim()}</Fact>
            </Row>
            <Fact label={tr.fleet.register.facts.hostKeyToPin}>{fingerprint ?? tr.fleet.register.facts.notConfirmed}</Fact>
            <Row gap="var(--sg-s-8)">
              <Fact label={tr.fleet.register.facts.tier}>{meta.tier.trim() || '—'}</Fact>
              <Fact label={tr.fleet.register.facts.role}>{meta.role.trim() || '—'}</Fact>
              <Fact label={tr.fleet.register.facts.guardianRole}>node</Fact>
              <Fact label={tr.fleet.register.facts.cliSupport}>{meta.cli_support.join(', ') || '—'}</Fact>
              <Fact label={tr.fleet.register.facts.tailscaleTags}>{meta.tailscale_tags.join(', ') || '—'}</Fact>
            </Row>
            <Fact label={tr.fleet.register.facts.description}>{meta.description.trim() || '—'}</Fact>
            <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>
              {tr.fleet.register.explain}
            </div>
            <Row>
              <Button variant="ghost" disabled={registering} onClick={() => setStep(2)}>
                {tr.common.back}
              </Button>
              <Button variant="primary" loading={registering} disabled={fingerprint === null} onClick={() => void register()}>
                {tr.fleet.register.registerNode}
              </Button>
            </Row>
          </>
        ) : null}
      </Stack>
    </Panel>
  );
}

/**
 * `#/fleet/nodes/new`: register a node from the dashboard (docs/design/node-registration.md, section 4).
 * Loads the hub's registration facts first and stops with the blockers when it cannot register.
 */
export function NodeRegisterPage() {
  const reg = useHubData(fetchNodeRegistration);
  const [run, setRun] = useState(0);
  const [prefill, setPrefill] = useState<MeshPrefill | undefined>(undefined);
  return (
    <Stack gap="var(--sg-s-8)">
      <MeshJoinPanel
        onPrefill={(p) => {
          // A new key remounts the wizard with the joined node's name, tailnet IP and tags.
          setPrefill(p);
          setRun((n) => n + 1);
        }}
      />
      <DataGate state={reg} what={tr.fleet.register.registrationWhat}>
        {(r) =>
          r.can_register ? (
            <Wizard
              key={run}
              reg={r}
              prefill={prefill}
              onAnother={() => {
                setPrefill(undefined);
                setRun((n) => n + 1);
              }}
            />
          ) : (
            <Blocked reg={r} />
          )
        }
      </DataGate>
    </Stack>
  );
}

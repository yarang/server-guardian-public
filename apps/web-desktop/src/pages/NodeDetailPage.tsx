import { tr } from '../i18n';
import { reachabilityInfo } from '../lib/reachability';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { fetchFleetSSHAudit } from '../api';
import { Ago } from '../components/Ago';
import { ErrorNotice, LoadingNote } from '../components/DataGate';
import { Notice } from '../components/Notice';
import { Row, Stack, mono, muted } from '../components/layout';
import { useRefreshToken } from '../hooks/refresh';
import { useHubAction } from '../hooks/useHubAction';
import { fmtTime } from '../lib/debate';
import { buildNodeDebateTask } from '../lib/debateTask';
import { fleetHref, navigate, navigateFleet } from '../router';
import { setDebatePrefill } from '../state/debatePrefill';
import { useHubStore } from '../state/hubStore';
import type { FleetNodeResult, FleetNode } from '../types';
import { EditNodeDialog } from './nodes/EditNodeDialog';
import { NodeMembership } from './nodes/NodeMembership';
import { ProbeOutcome, useNodeProbe } from './nodes/NodeForms';
import { PinHostKeyDialog } from './nodes/PinHostKeyDialog';
import { RemoveNodeDialog } from './nodes/RemoveNodeDialog';
import { RevokeKeyDialog } from './nodes/RevokeKeyDialog';
import { NodeDetails } from './ssh/FleetSshAudit';

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-1)', minWidth: 0 }}>
      <span style={{ ...muted, fontSize: 12, fontWeight: 600 }}>{label}</span>
      <span style={{ ...mono, overflowWrap: 'anywhere' }}>{children}</span>
    </div>
  );
}

function BackLink() {
  return <a href={fleetHref('nodes')}>{tr.fleet.detail.backToNodes}</a>;
}

const CONNECTION_LABEL: Record<string, string> = tr.fleet.detail.connectionLabel;

const HOST_KEY_LABEL: Record<string, string> = tr.fleet.detail.hostKeyLabel;

/** Where the node's connection settings come from and how its host key is trusted, with edit, test and remove. */
function ConnectionPanel({ node }: { node: FleetNode }) {
  const { servers, groups } = useHubStore();
  const [dialog, setDialog] = useState<'edit' | 'pin' | 'remove' | 'revoke' | null>(null);
  const probe = useNodeProbe();
  const isHub = node.guardian_role === 'hub';
  const reloadAll = () => {
    servers.reload();
    groups.reload();
  };

  return (
    <Panel
      title={tr.fleet.detail.connection}
      actions={
        <Row>
          <Button size="sm" variant="outline" onClick={() => setDialog('edit')}>
            {tr.fleet.detail.editNode}
          </Button>
          <Button size="sm" variant={node.host_key_mode === 'pinned' ? 'ghost' : 'outline'} onClick={() => setDialog('pin')}>
            {node.host_key_mode === 'pinned' ? tr.fleet.detail.reconfirmHostKey : tr.fleet.detail.pinHostKey}
          </Button>
          <Button size="sm" variant="outline" loading={probe.testing} onClick={() => void probe.test(node.id, servers.reload)}>
            {tr.fleet.detail.testConnection}
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={isHub}
            title={isHub ? tr.fleet.detail.revokeDisabledHub : tr.fleet.detail.revokeTitle}
            onClick={() => setDialog('revoke')}
          >
            {tr.fleet.detail.revokeHubKey}
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={isHub}
            title={isHub ? tr.fleet.detail.removeDisabledHub : undefined}
            onClick={() => setDialog('remove')}
          >
            {tr.fleet.detail.removeNode}
          </Button>
        </Row>
      }
    >
      <Stack gap="var(--sg-s-6)">
        <Row gap="var(--sg-s-8)">
          <Fact label={tr.fleet.detail.facts.user}>{node.user}</Fact>
          <Fact label={tr.fleet.detail.facts.port}>{String(node.port)}</Fact>
          <Fact label={tr.fleet.detail.facts.connectionSource}>{node.connection ? CONNECTION_LABEL[node.connection] ?? node.connection : '—'}</Fact>
          <Fact label={tr.fleet.detail.facts.hostKey}>{node.host_key_mode ? HOST_KEY_LABEL[node.host_key_mode] ?? node.host_key_mode : '—'}</Fact>
          {node.hub_key_revoked_at ? <Fact label={tr.fleet.detail.facts.hubKey}>{tr.fleet.detail.facts.revoked(fmtTime(node.hub_key_revoked_at))}</Fact> : null}
        </Row>
        {isHub ? <div style={{ ...muted, fontSize: 13 }}>{tr.fleet.detail.hubNotice}</div> : null}
        {probe.probe ? (
          <ProbeOutcome
            nodeId={node.id}
            probe={probe.probe}
            authorizedKey={probe.authorizedKey}
            keyBlocker={probe.keyBlocker}
            testing={probe.testing}
            testError={probe.error}
            onTestAgain={() => void probe.test(node.id, servers.reload)}
          />
        ) : probe.error ? (
          <Notice tone="crit">{probe.error}</Notice>
        ) : null}
      </Stack>
      {dialog === 'edit' ? (
        <EditNodeDialog node={node} fields={groups.data?.catalog?.fields ?? []} onClose={() => setDialog(null)} onSaved={reloadAll} />
      ) : null}
      {dialog === 'pin' ? <PinHostKeyDialog node={node} onClose={() => setDialog(null)} onSaved={servers.reload} /> : null}
      {dialog === 'revoke' ? (
        <RevokeKeyDialog
          node={node}
          onClose={() => {
            setDialog(null);
            servers.reload();
          }}
        />
      ) : null}
      {dialog === 'remove' ? (
        <RemoveNodeDialog
          node={node}
          onClose={() => setDialog(null)}
          onRevokeFirst={() => setDialog('revoke')}
          onRemoved={() => {
            navigateFleet('nodes');
            reloadAll();
          }}
        />
      ) : null}
    </Panel>
  );
}

function ProbePanel({ probe, running, error }: { probe: FleetNodeResult | null; running: boolean; error: string | null }) {
  const { diagnosis } = useHubStore();
  return (
    <Panel
      title={tr.fleet.detail.probe.title}
      actions={
        <Row>
          {diagnosis.measuredAt !== null ? (
            <span style={{ ...muted, fontSize: 12 }}>
              <Ago at={diagnosis.measuredAt} prefix={tr.fleet.detail.probe.probedPrefix} />
            </span>
          ) : null}
          <Button size="sm" variant="outline" loading={running} onClick={diagnosis.rerun}>
            {tr.fleet.detail.probe.rerun}
          </Button>
        </Row>
      }
    >
      {error !== null ? (
        <ErrorNotice title={tr.fleet.detail.probe.failed} message={error} onRetry={diagnosis.rerun} retrying={running} />
      ) : probe ? (
        <Row gap="var(--sg-s-8)">
          <StatusChip tone={reachabilityInfo(probe).tone}>{reachabilityInfo(probe).label}</StatusChip>
          <Fact label={tr.fleet.detail.facts.uptime}>{probe.uptime || '—'}</Fact>
          <Fact label={tr.fleet.detail.facts.load}>{probe.load_avg || '—'}</Fact>
          <Fact label={tr.fleet.detail.facts.memory}>{probe.memory || '—'}</Fact>
          <Fact label={tr.fleet.detail.facts.disk}>{probe.disk || '—'}</Fact>
          <Fact label={tr.fleet.detail.facts.probe}>{`${probe.duration_ms} ms`}</Fact>
        </Row>
      ) : running ? (
        <LoadingNote what={tr.fleet.detail.probe.loadingWhat} />
      ) : (
        <div style={muted}>{diagnosis.data ? tr.fleet.detail.probe.notInDiagnosis : tr.fleet.detail.probe.noDiagnosis}</div>
      )}
    </Panel>
  );
}

function Found({ node }: { node: FleetNode }) {
  const { groups, diagnosis } = useHubStore();
  const audit = useHubAction(fetchFleetSSHAudit);
  const refreshToken = useRefreshToken();
  const run = audit.run;
  const id = node.id;
  // Audit only this node (the hub caches per target set for 60 s); refresh re-audits on the global Refresh.
  useEffect(() => {
    void run([id], false);
  }, [run, id, refreshToken]);

  const probe = diagnosis.data?.results?.find((r) => r.id === id) ?? null;
  const auditNode = audit.data?.nodes.find((n) => n.id === id) ?? null;
  const memberOf = (groups.data?.groups ?? []).filter((g) => g.members.includes(id));

  const debateNode = () => {
    setDebatePrefill(buildNodeDebateTask(node, probe, auditNode), 'node', { node: id });
    navigate('debates');
  };

  return (
    <Stack>
      <Panel
        title={node.name}
        actions={
          <Row>
            <a href={fleetHref('commands', [], { target: id })}>{tr.fleet.detail.runOnNode}</a>
            <Button size="sm" variant="primary" onClick={debateNode}>
              {tr.fleet.detail.debateNode}
            </Button>
          </Row>
        }
      >
        <Stack gap="var(--sg-s-6)">
          <Row gap="var(--sg-s-8)">
            <Fact label={tr.fleet.detail.facts.host}>{node.hostname}</Fact>
            <Fact label={tr.fleet.detail.facts.arch}>{node.arch}</Fact>
            <Fact label={tr.fleet.detail.facts.meshIp}>{node.tailscale_ip ?? '—'}</Fact>
            <Fact label={tr.fleet.detail.facts.groups}>
              {groups.data === null ? (groups.error !== null ? tr.fleet.detail.groupsUnavailable : tr.fleet.detail.groupsLoading) : memberOf.length ? memberOf.map((g) => g.name).join(', ') : tr.fleet.detail.groupsNone}
            </Fact>
          </Row>
          <div>
            <BackLink />
          </div>
        </Stack>
      </Panel>

      <ConnectionPanel node={node} />

      <NodeMembership node={node} groups={groups} />

      <ProbePanel probe={probe} running={diagnosis.loading} error={diagnosis.error} />

      <Panel title={tr.fleet.detail.sshAudit}>
        {audit.error !== null ? (
          <ErrorNotice title={tr.fleet.detail.sshAuditFailed} message={audit.error} onRetry={() => void run([id], true)} retrying={audit.running} />
        ) : audit.data === null ? (
          <LoadingNote what={tr.fleet.detail.sshAuditLoadingWhat} />
        ) : auditNode ? (
          <NodeDetails node={auditNode} link={false} />
        ) : (
          <Notice tone="warn">{tr.fleet.detail.noAuditEntry}</Notice>
        )}
      </Panel>
    </Stack>
  );
}

/** `#/fleet/nodes/:id`: one node's inventory entry, probe result and SSH audit, with the actions that start from it. */
export function NodeDetailPage({ id }: { id: string }) {
  const { servers } = useHubStore();
  if (servers.error !== null) {
    return (
      <Stack>
        <ErrorNotice title={tr.fleet.detail.inventoryFailed} message={servers.error} onRetry={servers.reload} retrying={servers.loading} />
        <BackLink />
      </Stack>
    );
  }
  if (servers.data === null) return <LoadingNote what={tr.fleet.detail.inventoryWhat} />;
  const node = servers.data.find((s) => s.id === id);
  if (!node) {
    return (
      <Stack gap="var(--sg-s-5)">
        <Notice tone="warn">{tr.fleet.detail.notFound(id)}</Notice>
        <div>
          <BackLink />
        </div>
      </Stack>
    );
  }
  return <Found key={node.id} node={node} />;
}

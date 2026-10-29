import { tr } from '../i18n';
import { Panel, StatCard, StatusChip } from '@server-guardian/ui';
import { fetchAuthorizedKeys, fetchSSHConfig, fetchSSHSecurity } from '../api';
import { DataGate } from '../components/DataGate';
import { DataTable } from '../components/DataTable';
import { Disclosure } from '../components/Disclosure';
import { Grid, Stack } from '../components/layout';
import { useHubData } from '../hooks/useHubData';
import { scoreTone } from '../lib/tones';
import type { SSHConfigSummary } from '../types';
import { FleetSshAudit } from './ssh/FleetSshAudit';
import { KEY_COLUMNS, RuleList } from './ssh/shared';

function ConfigRow({ label, value, good }: { label: string; value: string | number; good?: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 'var(--sg-s-4)',
        padding: 'var(--sg-s-3) 0',
        borderBottom: '1px solid var(--sg-border-subtle)',
      }}
    >
      <span style={{ color: 'var(--sg-text-secondary)' }}>{label}</span>
      {good === undefined ? (
        <span style={{ fontFamily: 'var(--sg-font-mono)' }}>{String(value)}</span>
      ) : (
        <StatusChip tone={good ? 'ok' : 'crit'}>{String(value)}</StatusChip>
      )}
    </div>
  );
}

function ConfigSummary({ c }: { c: SSHConfigSummary }) {
  return (
    <div>
      <ConfigRow label={tr.fleet.ssh.config.port} value={c.port} />
      <ConfigRow label="PermitRootLogin" value={c.permit_root_login} good={c.permit_root_login === 'no'} />
      <ConfigRow label="PasswordAuthentication" value={c.password_authentication} good={c.password_authentication === 'no'} />
      <ConfigRow label="PubkeyAuthentication" value={c.pubkey_authentication} good={c.pubkey_authentication === 'yes'} />
      <ConfigRow label="MaxAuthTries" value={c.max_auth_tries} />
      <ConfigRow label="X11Forwarding" value={c.x11_forwarding} />
      <ConfigRow label="AllowTcpForwarding" value={c.allow_tcp_forwarding} />
      {c.syntax_checked ? (
        <ConfigRow label={tr.fleet.ssh.config.syntax} value={c.is_valid ? tr.fleet.ssh.config.valid : tr.fleet.ssh.config.invalid} good={c.is_valid} />
      ) : (
        <ConfigRow label={tr.fleet.ssh.config.syntax} value={tr.fleet.ssh.config.notChecked} />
      )}
      <ConfigRow label={tr.fleet.ssh.config.source} value={c.source || '—'} />
      {(!c.syntax_checked || !c.is_valid) && c.syntax_message ? (
        <p
          style={{
            margin: 'var(--sg-s-3) 0 0',
            color: c.syntax_checked ? 'var(--sg-crit)' : 'var(--sg-text-muted)',
            fontFamily: 'var(--sg-font-mono)',
            fontSize: 12,
          }}
        >
          {c.syntax_message}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The hub agent's own view of its sshd (/api/ssh/*). The hub also appears as a row of the fleet
 * audit; this section stays because it needs no fleet SSH (it works while the fleet key's agent is
 * down) and shows what the agent process itself sees.
 */
function HubLocalAudit() {
  const security = useHubData(fetchSSHSecurity);
  const config = useHubData(fetchSSHConfig);
  const keys = useHubData(fetchAuthorizedKeys);

  return (
    <Stack>
      <Grid min={300}>
        <Panel title={tr.fleet.ssh.scorePanel}>
          <DataGate state={security} what={tr.fleet.ssh.securityWhat}>
            {(s) => (
              <div style={{ display: 'grid', gap: 'var(--sg-s-5)' }}>
                <StatCard
                  label={tr.fleet.ssh.score}
                  value={s.score}
                  unit="/ 100"
                  caption={s.score >= 80 ? tr.fleet.ssh.good : s.score >= 50 ? tr.fleet.ssh.attention : tr.fleet.ssh.weak}
                  tone={scoreTone(s.score)}
                />
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-4)' }}>
                  <StatusChip tone="ok">{tr.fleet.ssh.passed(s.pass_count)}</StatusChip>
                  <StatusChip tone={s.failed_count > 0 ? 'crit' : 'idle'}>{tr.fleet.ssh.failed(s.failed_count)}</StatusChip>
                  {s.warn_count ? <StatusChip tone="warn">{tr.fleet.ssh.warnings(s.warn_count)}</StatusChip> : null}
                  {s.info_count ? <StatusChip tone="idle">{tr.fleet.ssh.info(s.info_count)}</StatusChip> : null}
                </div>
              </div>
            )}
          </DataGate>
        </Panel>
        <DataGate state={config} what={tr.fleet.ssh.configWhat}>
          {(c) => (
            <Panel title={tr.fleet.ssh.sshdConfig(c.config_path)}>
              <ConfigSummary c={c} />
            </Panel>
          )}
        </DataGate>
      </Grid>

      <Panel title={tr.fleet.ssh.auditRules}>
        <DataGate state={security} what={tr.fleet.ssh.rulesWhat}>
          {(s) => <RuleList rules={s.rules} empty={tr.fleet.ssh.noRules} />}
        </DataGate>
      </Panel>

      <Panel title={tr.fleet.ssh.keysTitle(keys.data?.length)}>
        <DataGate state={keys} what={tr.fleet.ssh.keysWhat}>
          {(list) => (
            <DataTable
              label={tr.fleet.ssh.keysLabel}
              columns={KEY_COLUMNS}
              rows={list}
              getKey={(k, i) => k.id || `${k.fingerprint}-${i}`}
              empty={tr.fleet.ssh.keysEmpty}
            />
          )}
        </DataGate>
      </Panel>
    </Stack>
  );
}

export function SshPage() {
  return (
    <Stack>
      <FleetSshAudit />
      <Disclosure summary={tr.fleet.ssh.hubAgentSummary}>
        <HubLocalAudit />
      </Disclosure>
    </Stack>
  );
}

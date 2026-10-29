import { tr } from '../../i18n';
import { StatusChip } from '@server-guardian/ui';
import type { Column } from '../../components/DataTable';
import { ruleTone, severityTone } from '../../lib/tones';
import type { AuthorizedKey, SecurityRuleCheck } from '../../types';

/** Audit rules as cards: status, title, severity, current vs expected and the remediation. */
export function RuleList({ rules, empty }: { rules: SecurityRuleCheck[]; empty: string }) {
  if (rules.length === 0) return <div style={{ color: 'var(--sg-text-muted)' }}>{empty}</div>;
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-5)' }}>
      {rules.map((r) => (
        <li
          key={r.rule_id}
          style={{
            display: 'grid',
            gap: 'var(--sg-s-2)',
            padding: 'var(--sg-s-5)',
            border: '1px solid var(--sg-border-subtle)',
            borderRadius: 'var(--sg-r-card)',
            background: 'var(--sg-surface-sunken)',
          }}
        >
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-4)' }}>
            <StatusChip tone={ruleTone(r.status)} title={r.status}>
              {tr.fleet.ssh.ruleStatus[r.status] ?? r.status}
            </StatusChip>
            <strong style={{ flex: 1, minWidth: 0 }}>{r.title}</strong>
            <StatusChip tone={severityTone(r.severity)} title={r.severity}>
              {tr.fleet.ssh.severity[String(r.severity).toLowerCase()] ?? r.severity}
            </StatusChip>
          </div>
          <div style={{ fontFamily: 'var(--sg-font-mono)', fontSize: 12, color: 'var(--sg-text-secondary)' }}>
            {tr.fleet.ssh.current}: {r.current_value || '—'} · {tr.fleet.ssh.expected}: {r.expected_value || '—'}
          </div>
          {r.remediation ? <div style={{ fontSize: 13, color: 'var(--sg-text-secondary)' }}>{tr.fleet.ssh.remediation}: {r.remediation}</div> : null}
        </li>
      ))}
    </ul>
  );
}

export const KEY_COLUMNS: Column<AuthorizedKey>[] = [
  { header: tr.fleet.ssh.keyColumns.type, mono: true, render: (k) => k.key_type },
  { header: tr.fleet.ssh.keyColumns.comment, render: (k) => k.comment || '—' },
  { header: tr.fleet.ssh.keyColumns.fingerprint, mono: true, render: (k) => k.fingerprint },
  { header: tr.fleet.ssh.keyColumns.options, mono: true, render: (k) => k.options || '—' },
];

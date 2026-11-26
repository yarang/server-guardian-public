import { useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { fetchResidentMailBody } from '../../api';
import { Ago } from '../../components/Ago';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Notice } from '../../components/Notice';
import { muted } from '../../components/layout';
import { parseTime } from '../../lib/debate';
import { errorText, fmtBytes } from '../../lib/format';
import type { ResidentMail, ResidentMailBody } from '../../types';
import { tr } from '../../i18n';
import { mailStateLabel, mailStateTone } from './format';

const preStyle = {
  margin: 0,
  padding: 'var(--sg-s-4)',
  background: 'var(--sg-surface)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  fontFamily: 'var(--sg-font-mono)',
  fontSize: 12,
  lineHeight: 1.5,
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  minWidth: 0,
} as const;

/**
 * The mail one agent sent or received. Bodies are not in the snapshot (they can be large): the
 * "Show body" button loads one mail on demand and shows it below the table, in a masked form.
 */
export function AgentMail({ mail }: { mail: ResidentMail[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [bodies, setBodies] = useState<Record<string, ResidentMailBody>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [error, setError] = useState('');

  const toggle = (m: ResidentMail) => {
    if (openId === m.id) {
      setOpenId(null);
      return;
    }
    setOpenId(m.id);
    setError('');
    if (!bodies[m.id]) {
      setLoadingId(m.id);
      fetchResidentMailBody(m.id)
        .then((r) => setBodies((prev) => ({ ...prev, [m.id]: r })))
        .catch((err) => setError(errorText(err)))
        .finally(() => setLoadingId(null));
    }
  };

  const columns: Column<ResidentMail>[] = [
    {
      header: tr.residents.colCreated,
      nowrap: true,
      render: (m) => {
        const t = m.created ? parseTime(m.created) : null;
        return t !== null ? <Ago at={t} /> : <span style={muted}>{m.created ?? tr.common.dash}</span>;
      },
    },
    {
      header: tr.residents.colRoute,
      mono: true,
      render: (m) => (
        <span style={{ whiteSpace: 'nowrap' }}>
          {m.sender ?? '?'} → {m.recipient ?? '?'}
        </span>
      ),
    },
    { header: tr.residents.colKind, nowrap: true, mono: true, render: (m) => m.kind ?? tr.common.dash },
    { header: tr.residents.colSubject, minWidth: 160, render: (m) => m.subject ?? '' },
    {
      header: tr.residents.colState,
      nowrap: true,
      render: (m) => (
        <StatusChip tone={mailStateTone(m.state)} title={m.state ?? undefined}>
          {mailStateLabel(m.state)}
        </StatusChip>
      ),
    },
    { header: tr.residents.colBytes, nowrap: true, align: 'right', render: (m) => (m.bytes !== null ? fmtBytes(m.bytes) : tr.common.dash) },
    {
      header: '',
      render: (m) => (
        <Button size="sm" variant="outline" loading={loadingId === m.id} onClick={() => toggle(m)}>
          {openId === m.id ? tr.residents.hideBody : tr.residents.showBody}
        </Button>
      ),
    },
  ];

  const open = openId !== null ? mail.find((m) => m.id === openId) : undefined;
  const body = openId !== null ? bodies[openId] : undefined;
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-4)', minWidth: 0 }}>
      <DataTable label={tr.residents.mailTitle} columns={columns} rows={mail} getKey={(m) => m.id} empty={tr.residents.noMail} />
      {error ? <Notice tone="crit">{tr.residents.bodyFailed(error)}</Notice> : null}
      {open && body ? (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)', minWidth: 0 }}>
          <div style={{ ...muted, fontSize: 12 }}>
            {tr.residents.bodyNote(fmtBytes(body.bytes ?? open.bytes ?? 0))} ({open.id})
          </div>
          <pre style={preStyle}>{body.body ?? ''}</pre>
        </div>
      ) : null}
    </div>
  );
}

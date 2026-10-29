import { useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { confirmCommandSet, AuthRequiredError } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { errorText } from '../../lib/format';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import type { CommandSet, CommandSetList } from '../../types';
import { CommandSetDialog, NEW_COMMAND_SET } from './CommandSetDialog';
import { tr } from '../../i18n';
import { riskLabel } from '../../lib/debate';

const RISK_TONE = { low: 'ok', medium: 'warn', high: 'crit' } as const;

/** The stored command sets (docs/design/batch-execution.md, B4c): builtin ones to read, the operator's own to edit. */
export function CommandSetsSection({ state, onChanged }: { state: HubDataState<CommandSetList>; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ set: CommandSet; isNew: boolean } | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const closeConfirm = () => {
    setConfirming(null);
    setConfirmError(null);
  };
  const confirmSet = async (id: string) => {
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      await confirmCommandSet(id);
      setBanner(tr.debate.commandSet.confirmed(id));
      setConfirming(null);
      onChanged();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setConfirmError(errorText(err));
    } finally {
      setConfirmBusy(false);
    }
  };
  const staleDays = new Map((state.data?.stale ?? []).map((s) => [s.id, s.age_days]));
  const count = state.data?.sets?.length;

  return (
    <Panel
      title={count !== undefined ? tr.debate.commandSet.sectionCount(count) : tr.debate.commandSet.section}
      actions={
        <Row>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setBanner(null);
              state.reload();
            }}
          >
            {tr.common.refresh}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setEditing({ set: NEW_COMMAND_SET, isNew: true })}>
            {tr.debate.commandSet.newButton}
          </Button>
        </Row>
      }
    >
      <Stack gap="var(--sg-s-6)">
        <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{tr.debate.commandSet.intro}</div>
        {banner ? <Notice tone="ok">{banner}</Notice> : null}
        <DataGate state={state} what={tr.debate.commandSet.what}>
          {(data) => (
            <Grid min={300} gap="var(--sg-s-6)">
              {(data.sets || []).map((s) => (
                <Card key={s.id}>
                  <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
                    <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{s.id}</strong>
                    <StatusChip tone={s.source === 'builtin' ? 'idle' : 'info'}>
                      {s.source === 'builtin' ? tr.debate.commandSet.builtinChip : tr.debate.commandSet.operatorChip}
                    </StatusChip>
                  </Row>
                  <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{s.description}</div>
                  <Row gap="var(--sg-s-3)">
                    <StatusChip tone={RISK_TONE[s.risk] ?? 'idle'}>{riskLabel(s.risk)}</StatusChip>
                    <StatusChip tone={s.reversible ? 'ok' : 'warn'}>{s.reversible ? tr.debate.commandSet.undoable : tr.debate.commandSet.irreversible}</StatusChip>
                    {s.needs_sudo ? <StatusChip tone="info">{tr.debate.commandSet.sudo}</StatusChip> : null}
                    {s.source === 'operator' ? (
                      <StatusChip tone={s.confirmed ? 'ok' : 'warn'}>
                        {s.confirmed ? tr.debate.commandSet.autoConfirmed : tr.debate.commandSet.autoUnconfirmed}
                      </StatusChip>
                    ) : null}
                  </Row>
                  <div style={{ ...muted, fontSize: 12 }}>
                    {s.reviewed ? tr.debate.commandSet.reviewed(s.reviewed) : null}
                    {staleDays.has(s.id) ? ` | ${tr.debate.commandSet.stale(staleDays.get(s.id) ?? 0)}` : ''}
                  </div>
                  <Row gap="var(--sg-s-3)">
                    <Button
                      size="sm"
                      variant="outline"
                      aria-label={s.source === 'builtin' ? tr.debate.commandSet.viewAria(s.id) : tr.debate.commandSet.editAria(s.id)}
                      onClick={() => setEditing({ set: s, isNew: false })}
                    >
                      {s.source === 'builtin' ? tr.debate.commandSet.viewButton : tr.common.edit}
                    </Button>
                    {s.source === 'operator' && !s.confirmed ? (
                      <Button size="sm" variant="secondary" aria-label={tr.debate.commandSet.confirmAria(s.id)} onClick={() => setConfirming(s.id)}>
                        {tr.debate.commandSet.confirmButton}
                      </Button>
                    ) : null}
                  </Row>
                </Card>
              ))}
            </Grid>
          )}
        </DataGate>
      </Stack>
      {confirming ? (
        <ConfirmModal
          tone="primary"
          title={tr.debate.commandSet.confirmTitle}
          message={tr.debate.commandSet.confirmMessage(confirming)}
          confirmLabel={tr.debate.commandSet.confirmButton}
          busy={confirmBusy}
          error={confirmError}
          onConfirm={() => void confirmSet(confirming)}
          onCancel={closeConfirm}
        />
      ) : null}
      {editing ? (
        <CommandSetDialog
          set={editing.set}
          isNew={editing.isNew}
          takenIds={(state.data?.sets ?? []).map((s) => s.id)}
          onClose={() => setEditing(null)}
          onSaved={(res) => {
            setBanner(tr.debate.commandSet.saved(res.id));
            setEditing(null);
            onChanged();
          }}
          onDeleted={(id) => {
            setBanner(tr.debate.commandSet.deleted(id));
            setEditing(null);
            onChanged();
          }}
        />
      ) : null}
    </Panel>
  );
}

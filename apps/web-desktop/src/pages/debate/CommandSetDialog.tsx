import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deleteCommandSet, saveCommandSet } from '../../api';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack } from '../../components/layout';
import { errorText } from '../../lib/format';
import type { CommandSet, SaveResult } from '../../types';
import { tr } from '../../i18n';

const OSES = ['linux', 'macos'] as const;
const RISKS = ['low', 'medium', 'high'] as const;

export const NEW_COMMAND_SET: CommandSet = {
  id: '',
  description: '',
  os: ['linux'],
  needs_sudo: false,
  params: {},
  check: '',
  script: '',
  reversible: false,
  undo: '',
  risk: 'medium',
  timeout: 600,
};

/**
 * Create / edit / delete one of the operator's command sets. A builtin set opens read-only for the id it has: a
 * different id saves a copy of the operator's own. `fromScript` marks a set prefilled from a plan's ad-hoc script.
 */
export function CommandSetDialog({
  set,
  isNew,
  fromScript = false,
  takenIds = [],
  onClose,
  onSaved,
  onDeleted,
}: {
  set: CommandSet;
  isNew: boolean;
  fromScript?: boolean;
  /** Ids that already exist: a new set (or a copy of a builtin one) must not silently replace one of them. */
  takenIds?: string[];
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
  onDeleted: (id: string) => void;
}) {
  const isBuiltin = !isNew && set.source === 'builtin';
  const isOperator = !isNew && set.source === 'operator';
  const [id, setId] = useState(set.id);
  const [description, setDescription] = useState(set.description);
  const [os, setOs] = useState<string[]>(set.os);
  const [risk, setRisk] = useState<string>(set.risk);
  const [timeout, setTimeoutText] = useState(String(set.timeout));
  const [needsSudo, setNeedsSudo] = useState(set.needs_sudo);
  const [checkSudo, setCheckSudo] = useState(Boolean(set.check_sudo));
  const [reversible, setReversible] = useState(set.reversible);
  const [undo, setUndo] = useState(set.undo);
  const [check, setCheck] = useState(set.check);
  const [script, setScript] = useState(set.script);
  const [params, setParams] = useState(JSON.stringify(set.params, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const save = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(params.trim() || '{}');
    } catch {
      parsed = null;
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError(tr.debate.commandSet.badParams);
      return;
    }
    if (!isOperator && takenIds.includes(id.trim())) {
      setError(tr.debate.commandSet.idTaken(id.trim()));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await saveCommandSet({
        id: id.trim(),
        description,
        os,
        needs_sudo: needsSudo,
        check_sudo: needsSudo && checkSudo,
        params: parsed as CommandSet['params'],
        check,
        script,
        reversible: risk === 'high' ? false : reversible,
        undo,
        risk: risk as CommandSet['risk'],
        timeout: Number(timeout),
      });
      onSaved(result);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteCommandSet(set.id);
      onDeleted(set.id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(errorText(err));
      setBusy(false);
    }
  };

  const sub = fromScript ? tr.debate.commandSet.fromScript : isNew ? tr.debate.commandSet.subNew : isBuiltin ? tr.debate.commandSet.subBuiltin : tr.debate.commandSet.subOperator;
  const title = isNew ? tr.debate.commandSet.newTitle : isBuiltin ? tr.debate.commandSet.viewTitle(set.id) : tr.debate.commandSet.editTitle(set.id);
  const toggleOs = (name: string) => setOs((cur) => (cur.includes(name) ? cur.filter((o) => o !== name) : [...cur, name]));

  return (
    <>
      <Modal title={title} width={700} onClose={busy ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{sub}</div>
          <TextField label={tr.debate.commandSet.id} mono value={id} readOnly={isOperator} onChange={(e) => setId(e.target.value)} />
          <TextField label={tr.debate.commandSet.description} value={description} onChange={(e) => setDescription(e.target.value)} />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', alignItems: 'flex-end' }}>
            <SelectField
              label={tr.debate.commandSet.risk}
              value={risk}
              options={RISKS.map((r) => ({ value: r, label: r === 'low' ? tr.debate.commandSet.riskLow : r === 'medium' ? tr.debate.commandSet.riskMedium : tr.debate.commandSet.riskHigh }))}
              onChange={(e) => setRisk(e.target.value)}
            />
            <TextField label={tr.debate.commandSet.timeout} type="number" min={1} max={3600} value={timeout} onChange={(e) => setTimeoutText(e.target.value)} />
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)' }} role="group" aria-label={tr.debate.commandSet.os}>
            {OSES.map((name) => (
              <CheckboxField key={name} checked={os.includes(name)} onChange={() => toggleOs(name)}>
                {name}
              </CheckboxField>
            ))}
          </div>
          <Stack gap="var(--sg-s-3)">
            <CheckboxField checked={needsSudo} onChange={(e) => setNeedsSudo(e.target.checked)}>
              {tr.debate.commandSet.needsSudo}
            </CheckboxField>
            {needsSudo ? (
              <CheckboxField checked={checkSudo} onChange={(e) => setCheckSudo(e.target.checked)}>
                {tr.debate.commandSet.checkSudo}
              </CheckboxField>
            ) : null}
            <CheckboxField checked={risk === 'high' ? false : reversible} disabled={risk === 'high'} onChange={(e) => setReversible(e.target.checked)}>
              {tr.debate.commandSet.reversible}
            </CheckboxField>
          </Stack>
          <TextField label={tr.debate.commandSet.undo} value={undo} onChange={(e) => setUndo(e.target.value)} />
          <TextAreaField label={tr.debate.commandSet.check} mono rows={3} value={check} onChange={(e) => setCheck(e.target.value)} />
          <TextAreaField label={tr.debate.commandSet.script} mono rows={10} value={script} onChange={(e) => setScript(e.target.value)} />
          <TextAreaField label={tr.debate.commandSet.params} mono rows={4} value={params} onChange={(e) => setParams(e.target.value)} />
          {error ? <Notice tone="crit">{error}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {isOperator ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.debate.commandSet.deleteAction}
                </Button>
              ) : null}
            </div>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)' }}>
              <Button variant="ghost" disabled={busy} onClick={onClose}>
                {tr.common.cancel}
              </Button>
              <Button variant="primary" loading={busy} disabled={isBuiltin && id.trim() === set.id} onClick={() => void save()}>
                {tr.debate.commandSet.save}
              </Button>
            </div>
          </div>
        </Stack>
      </Modal>
      {confirmDelete ? (
        <ConfirmModal
          title={tr.debate.commandSet.deleteAction}
          message={tr.debate.commandSet.deleteConfirm(set.id)}
          confirmLabel={tr.debate.commandSet.deleteAction}
          busy={busy}
          error={deleteError}
          onConfirm={() => void remove()}
          onCancel={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        />
      ) : null}
    </>
  );
}

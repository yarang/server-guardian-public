import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deletePersona, savePersona } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack } from '../../components/layout';
import { errorText } from '../../lib/format';
import type { PersonaConfig, SaveResult } from '../../types';
import { tr } from '../../i18n';

const BASE_ROLES = ['Domain Expert Agent', 'Critic Agent', 'JEV-MCP Decision Agent'];

export const NEW_PERSONA: PersonaConfig = {
  id: '',
  name: 'New Custom Agent',
  role: 'Domain Expert Agent',
  domain: 'Custom Engineering Domain',
  system_prompt: '',
};

function subtitleFor(isNew: boolean, source: PersonaConfig['source']): string {
  if (isNew) return tr.debate.persona.subNew;
  if (source === 'builtin')
    return tr.debate.persona.subBuiltin;
  return tr.debate.persona.subCustom;
}

/** Create / edit / delete a persona. Hub validation errors stay inside the dialog. */
export function PersonaDialog({
  persona,
  isNew,
  onClose,
  onSaved,
  onDeleted,
}: {
  persona: PersonaConfig;
  isNew: boolean;
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
  onDeleted: (id: string) => void;
}) {
  const isBuiltin = !isNew && persona.source === 'builtin';
  const isCustom = !isNew && persona.source === 'custom';
  const roles = persona.role && !BASE_ROLES.includes(persona.role) ? [...BASE_ROLES, persona.role] : BASE_ROLES;

  const [id, setId] = useState(persona.id);
  const [name, setName] = useState(persona.name);
  const [role, setRole] = useState(persona.role);
  const [domain, setDomain] = useState(persona.domain);
  const [prompt, setPrompt] = useState(persona.system_prompt);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await savePersona({ ...persona, id: id.trim(), name, role, domain, system_prompt: prompt });
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
      await deletePersona(persona.id);
      onDeleted(persona.id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <>
      <Modal title={isNew ? tr.debate.persona.newTitle : tr.debate.persona.editTitle(persona.name)} width={600} onClose={busy ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{subtitleFor(isNew, persona.source)}</div>
          <TextField label={tr.debate.persona.id} mono value={id} readOnly={!isNew} onChange={(e) => setId(e.target.value)} />
          <TextField label={tr.debate.persona.name} value={name} onChange={(e) => setName(e.target.value)} />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)' }}>
            <SelectField label={tr.debate.persona.role} value={role} options={roles.map((r) => ({ value: r, label: r }))} onChange={(e) => setRole(e.target.value)} />
          </div>
          <TextField label={tr.debate.persona.domain} value={domain} onChange={(e) => setDomain(e.target.value)} />
          <TextAreaField label={tr.debate.persona.systemPrompt} mono rows={8} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          {error ? <Notice tone="crit">{error}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {isCustom ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.debate.persona.deleteCustom}
                </Button>
              ) : null}
            </div>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)' }}>
              <Button variant="ghost" disabled={busy} onClick={onClose}>
                {tr.common.cancel}
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void save()}>
                {isBuiltin ? tr.debate.persona.saveOverride : tr.common.save}
              </Button>
            </div>
          </div>
        </Stack>
      </Modal>
      {confirmDelete ? (
        <ConfirmModal
          title={tr.debate.persona.deleteCustom}
          message={tr.debate.persona.deleteConfirm(persona.id)}
          confirmLabel={tr.debate.persona.deleteAction}
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

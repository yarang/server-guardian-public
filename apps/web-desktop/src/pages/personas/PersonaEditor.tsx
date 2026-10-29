import { useMemo, useState } from 'react';
import { Button, Panel, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deletePersona, savePersona } from '../../api';
import { Caption, Card } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Row, Stack, mono, muted } from '../../components/layout';
import { riskLabel, riskTone } from '../../lib/debate';
import { errorText } from '../../lib/format';
import {
  TEMPLATE_KEYS,
  blankDraft,
  draftFromCopy,
  draftFromPersona,
  draftFromTemplate,
  personaPayload,
  personaUsage,
  personasHref,
  roleOptions,
} from '../../lib/personas';
import type { PersonaDraft, TemplateKey } from '../../lib/personas';
import type { PersonaConfig, SaveResult, TeamList } from '../../types';
import { SourceChip } from '../debate/SourceChip';
import { hrefFor } from '../../router';
import { tr } from '../../i18n';

const ROLE_LIST_ID = 'persona-role-options';
const P = tr.personas;

function subtitleFor(isNew: boolean, source: PersonaConfig['source']): string {
  if (isNew) return tr.debate.persona.subNew;
  return source === 'builtin' ? tr.debate.persona.subBuiltin : tr.debate.persona.subCustom;
}

/** Which teams use the persona (teams reference it as an expert, proposer, critic or judge). */
function UsedBy({ personaId, teams, teamsFailed }: { personaId: string; teams: TeamList | null; teamsFailed: boolean }) {
  const uses = useMemo(() => (teams ? personaUsage(personaId, teams.teams) : []), [personaId, teams]);
  return (
    <Card>
      <Caption>{P.usedBy}</Caption>
      {teamsFailed ? (
        <div style={{ ...muted, fontSize: 13 }}>{P.usedByUnknown}</div>
      ) : teams === null ? null : uses.length === 0 ? (
        <div style={{ ...muted, fontSize: 13 }}>{P.usedByNone}</div>
      ) : (
        <>
          <div style={{ ...muted, fontSize: 12 }}>{P.usedByNote}</div>
          <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', display: 'grid', gap: 'var(--sg-s-2)' }}>
            {uses.map((u) => (
              <li key={u.teamId} style={{ overflowWrap: 'anywhere' }}>
                <a href={hrefFor('settings', ['teams'])}>{u.teamName}</a>{' '}
                <span style={{ ...mono, ...muted, fontSize: 12 }}>{u.teamId}</span>{' '}
                <span style={{ ...muted, fontSize: 12 }}>{`(${u.slots.map((s) => P.slot[s]).join(', ')})`}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

/** mcp_capabilities as checkboxes over the hub's tool catalog (the one the team editor uses); saved unknown names stay visible. */
function ToolChecklist({ catalog, selected, onChange }: { catalog: TeamList['catalog'] | null; selected: string[]; onChange: (tools: string[]) => void }) {
  const known = (catalog?.tools ?? []).map((t) => t.name);
  const rows = [
    ...(catalog?.tools ?? []),
    ...selected.filter((t) => !known.includes(t)).map((name) => ({ name, risk: 'unknown', description: tr.debate.teamDialog.notInCatalog })),
  ];
  return (
    <Card>
      <Caption>{P.fields.tools}</Caption>
      <div style={{ ...muted, fontSize: 12 }}>{P.fields.toolsHint}</div>
      {catalog === null ? <Notice tone="warn">{P.fields.toolsUnavailable}</Notice> : null}
      <Stack gap="var(--sg-s-3)">
        {rows.map((t) => (
          <CheckboxField
            key={t.name}
            checked={selected.includes(t.name)}
            onChange={(e) => onChange(e.target.checked ? [...selected, t.name] : selected.filter((x) => x !== t.name))}
          >
            <span style={{ ...mono, fontSize: 13 }}>{t.name}</span> <StatusChip tone={riskTone(t.risk)}>{riskLabel(t.risk)}</StatusChip>
            <span style={{ display: 'block', ...muted, fontSize: 12 }}>{t.description}</span>
          </CheckboxField>
        ))}
      </Stack>
    </Card>
  );
}

/**
 * Full-page persona editor with every field the hub stores: id (new only), name, role, domain,
 * system prompt, focus areas, critique focus and tools. New personas can start from a template or a copy.
 * Hub validation errors stay on the page.
 */
export function PersonaEditor({
  persona,
  personas,
  teams,
  teamsFailed,
  onSaved,
  onDeleted,
  onCancel,
}: {
  /** The persona being edited; null for a new one. */
  persona: PersonaConfig | null;
  personas: PersonaConfig[];
  teams: TeamList | null;
  teamsFailed: boolean;
  onSaved: (result: SaveResult) => void;
  onDeleted: (id: string) => void;
  onCancel: () => void;
}) {
  const isNew = persona === null;
  const isBuiltin = !isNew && persona.source === 'builtin';
  const isCustom = !isNew && persona.source === 'custom';

  const [draft, setDraft] = useState<PersonaDraft>(() => (persona ? draftFromPersona(persona) : blankDraft()));
  const [startFrom, setStartFrom] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const patch = (p: Partial<PersonaDraft>) => setDraft((d) => ({ ...d, ...p }));
  const roles = useMemo(() => roleOptions(personas, draft.role), [personas, draft.role]);

  const pickStartFrom = (id: string) => {
    setStartFrom(id);
    const source = personas.find((p) => p.id === id);
    if (source) setDraft(draftFromCopy(source));
  };
  const applyTemplate = (key: TemplateKey) => {
    setStartFrom('');
    setDraft((d) => draftFromTemplate(key, d));
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await savePersona(personaPayload(draft, persona ?? undefined)));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!persona) return;
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

  const title = isNew ? P.newTitle : P.editTitle(persona.name);

  return (
    <Stack>
      <div>
        <a href={personasHref()}>{P.backToList}</a>
      </div>
      <Panel title={title} actions={persona ? <SourceChip source={persona.source} /> : undefined}>
        <Stack gap="var(--sg-s-6)">
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{subtitleFor(isNew, persona?.source)}</div>

          {isNew ? (
            <Card>
              <Row style={{ alignItems: 'flex-end' }}>
                <SelectField
                  label={P.startFrom}
                  value={startFrom}
                  options={[{ value: '', label: P.startFromNone }, ...personas.map((p) => ({ value: p.id, label: `${p.name} (${p.id})` }))]}
                  onChange={(e) => pickStartFrom(e.target.value)}
                />
                <div role="group" aria-label={P.templates} style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
                  {TEMPLATE_KEYS.map((k) => (
                    <Button key={k} size="sm" variant="outline" onClick={() => applyTemplate(k)}>
                      {P.template[k].label}
                    </Button>
                  ))}
                </div>
              </Row>
              <div style={{ ...muted, fontSize: 12 }}>{`${P.startFromHint} ${P.templatesHint}`}</div>
            </Card>
          ) : null}

          <TextField label={P.fields.id} mono value={draft.id} readOnly={!isNew} hint={isNew ? P.fields.idHint : undefined} onChange={(e) => patch({ id: e.target.value })} />
          <TextField label={P.fields.name} value={draft.name} onChange={(e) => patch({ name: e.target.value })} />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)' }}>
            <div style={{ flex: '1 1 260px', minWidth: 0 }}>
              <TextField label={P.fields.role} list={ROLE_LIST_ID} hint={P.fields.roleHint} value={draft.role} onChange={(e) => patch({ role: e.target.value })} />
              <datalist id={ROLE_LIST_ID}>
                {roles.map((r) => (
                  <option key={r} value={r} />
                ))}
              </datalist>
            </div>
          </div>
          <TextField label={P.fields.domain} value={draft.domain} onChange={(e) => patch({ domain: e.target.value })} />
          <TextAreaField label={P.fields.systemPrompt} mono rows={12} value={draft.prompt} onChange={(e) => patch({ prompt: e.target.value })} />
          <TextAreaField label={P.fields.focusAreas} rows={4} hint={P.fields.linesHint} value={draft.focusAreas} onChange={(e) => patch({ focusAreas: e.target.value })} />
          <TextAreaField label={P.fields.critiqueFocus} rows={4} hint={P.fields.linesHint} value={draft.critiqueFocus} onChange={(e) => patch({ critiqueFocus: e.target.value })} />
          <ToolChecklist catalog={teams?.catalog ?? null} selected={draft.tools} onChange={(tools) => patch({ tools })} />
          {!isNew ? <UsedBy personaId={persona.id} teams={teams} teamsFailed={teamsFailed} /> : null}

          {error ? <Notice tone="crit">{error}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {isCustom ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.debate.persona.deleteCustom}
                </Button>
              ) : null}
            </div>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
              <Button variant="ghost" disabled={busy} onClick={onCancel}>
                {tr.common.cancel}
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void save()}>
                {isBuiltin ? tr.debate.persona.saveOverride : isNew ? P.createButton : tr.common.save}
              </Button>
            </div>
          </div>
        </Stack>
      </Panel>
      {confirmDelete && persona ? (
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
    </Stack>
  );
}

import { useMemo, useState } from 'react';
import { Button, Panel, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deletePersona, savePersona, suggestPersona } from '../../api';
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
  characterIdTaken,
  draftFromCopy,
  draftFromPersona,
  draftFromSuggestion,
  draftFromTemplate,
  isCharacter,
  personaPayload,
  personaUsage,
  personasHref,
  roleOptions,
} from '../../lib/personas';
import { CharacterReadonly } from './CharacterReadonly';
import type { PersonaDraft, TemplateKey } from '../../lib/personas';
import type { PersonaConfig, PersonaSuggestResult, SaveResult, SuggestKind, SuggestSeat, TeamList } from '../../types';
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
                <a href={hrefFor('settings', ['flows'])}>{u.teamName}</a>{' '}
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
    ...selected.filter((t) => !known.includes(t)).map((name) => ({ name, risk: 'unknown', description: tr.debate.persona.notInCatalog })),
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

const SEATS: SuggestSeat[] = ['expert', 'proposer', 'critic', 'judge'];
const KINDS: SuggestKind[] = ['code', 'ops', 'design'];
const PROVIDERS = ['claude', 'codex', 'agy'];

/** The seat a draft's role label points at, for the suggest form's default. */
function seatOf(role: string): SuggestSeat {
  const r = role.toLowerCase();
  if (r.includes('critic')) return 'critic';
  if (r.includes('decision') || r.includes('judge')) return 'judge';
  return 'expert';
}

/**
 * "Suggest with AI": a brief, seat, kind and model go to the hub, one model turn drafts the persona and its
 * profile, and the draft is loaded into the form. Nothing is saved here.
 */
function SuggestCard({ draft, onSuggested }: { draft: PersonaDraft; onSuggested: (result: PersonaSuggestResult) => void }) {
  const S = P.suggest;
  const [brief, setBrief] = useState('');
  const [seat, setSeat] = useState<SuggestSeat>(() => seatOf(draft.role));
  const [kind, setKind] = useState<SuggestKind>('code');
  const [provider, setProvider] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PersonaSuggestResult | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await suggestPersona({
        brief: brief.trim(),
        seat,
        kind,
        provider: provider || undefined,
        current: draft.prompt.trim() ? { name: draft.name, domain: draft.domain, system_prompt: draft.prompt } : undefined,
      });
      setResult(res);
      onSuggested(res);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <Caption>{S.title}</Caption>
      <div style={{ ...muted, fontSize: 12 }}>{S.intro}</div>
      <TextAreaField label={S.brief} rows={2} maxLength={1000} placeholder={S.briefPlaceholder} value={brief} onChange={(e) => setBrief(e.target.value)} />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-4)', alignItems: 'flex-end' }}>
        <SelectField label={S.seat} value={seat} options={SEATS.map((v) => ({ value: v, label: P.slot[v] }))} onChange={(e) => setSeat(e.target.value as SuggestSeat)} />
        <SelectField label={S.kind} value={kind} options={KINDS.map((v) => ({ value: v, label: S.kinds[v] }))} onChange={(e) => setKind(e.target.value as SuggestKind)} />
        <SelectField
          label={S.provider}
          value={provider}
          options={[{ value: '', label: S.providerAuto }, ...PROVIDERS.map((v) => ({ value: v, label: v }))]}
          onChange={(e) => setProvider(e.target.value)}
        />
        <Button variant="outline" loading={busy} disabled={brief.trim() === ''} onClick={() => void run()}>
          {S.button}
        </Button>
      </div>
      <div style={{ ...muted, fontSize: 12 }}>{S.replaceNote}</div>
      {busy ? <Notice tone="info">{S.busy}</Notice> : null}
      {error ? <Notice tone="crit">{error}</Notice> : null}
      {result ? <Notice tone="ok">{S.applied(result.provider)}</Notice> : null}
      {result && result.warnings.length > 0 ? (
        <Notice tone="warn">
          {S.warnings}: {result.warnings.join(' / ')}
        </Notice>
      ) : null}
    </Card>
  );
}

/**
 * Full-page persona editor with every field the hub stores: id (new only), name, role, domain,
 * system prompt, focus areas, critique focus and tools. New personas can start from a template or a copy.
 * Hub validation errors stay on the page. Format 1 only: a character (format 2) gets `CharacterPage`.
 */
function PersonaForm({
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

  // A create form must never overwrite a character: an id that belongs to one is refused before the hub is asked.
  const idTaken = isNew && characterIdTaken(personas, draft.id);

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
    if (idTaken) return; // the button is disabled; this also holds when the click arrives anyway
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
                  // A character (format 2) cannot fill the form, so it is not offered as a starting point.
                  options={[
                    { value: '', label: P.startFromNone },
                    ...personas.filter((p) => !isCharacter(p)).map((p) => ({ value: p.id, label: `${p.name} (${p.id})` })),
                  ]}
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

          {/* The first call runs inside the suggest flow's try/catch, so a suggestion the form cannot load (a character)
              is shown as its error instead of breaking the render; the updater then applies it to the latest draft,
              keeping tool edits made while the model was answering. */}
          <SuggestCard
            draft={draft}
            onSuggested={(res) => {
              draftFromSuggestion(res.suggestion, draft, isNew);
              setDraft((d) => draftFromSuggestion(res.suggestion, d, isNew));
            }}
          />

          <TextField label={P.fields.id} mono value={draft.id} readOnly={!isNew} hint={isNew ? P.fields.idHint : undefined} error={idTaken ? P.fields.idTakenByCharacter : undefined} onChange={(e) => patch({ id: e.target.value })} />
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
          <TextAreaField label={P.fields.systemPrompt} mono rows={12} hint={draft.roleCard ? P.fields.roleCardHint(P.slot[draft.roleCard]) : undefined} value={draft.prompt} onChange={(e) => patch({ prompt: e.target.value })} />
          <TextAreaField label={P.fields.focusAreas} rows={4} hint={P.fields.linesHint} value={draft.focusAreas} onChange={(e) => patch({ focusAreas: e.target.value })} />
          <TextAreaField label={P.fields.critiqueFocus} rows={4} hint={P.fields.linesHint} value={draft.critiqueFocus} onChange={(e) => patch({ critiqueFocus: e.target.value })} />
          <TextField label={P.fields.tagline} maxLength={120} hint={P.fields.taglineHint} value={draft.tagline} onChange={(e) => patch({ tagline: e.target.value })} />
          <TextAreaField label={P.fields.look} rows={2} maxLength={200} hint={P.fields.lookHint} value={draft.look} onChange={(e) => patch({ look: e.target.value })} />
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
              <Button variant="primary" loading={busy} disabled={idTaken} onClick={() => void save()}>
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

/**
 * A character (format 2) on its own page: the read-only facts, the teams that use it and — for a custom one —
 * the delete button (delete is format-agnostic). No form and no save: the character editor is phase C4, and a
 * format 1 payload must never overwrite a character file.
 */
function CharacterPage({
  persona,
  teams,
  teamsFailed,
  onDeleted,
  onCancel,
}: {
  persona: PersonaConfig;
  teams: TeamList | null;
  teamsFailed: boolean;
  onDeleted: (id: string) => void;
  onCancel: () => void;
}) {
  const isCustom = persona.source === 'custom';
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

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
    <Stack>
      <div>
        <a href={personasHref()}>{P.backToList}</a>
      </div>
      <Panel title={P.character.title(persona.name)} actions={<SourceChip source={persona.source} />}>
        <Stack gap="var(--sg-s-6)">
          <CharacterReadonly persona={persona} />
          <UsedBy personaId={persona.id} teams={teams} teamsFailed={teamsFailed} />
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {isCustom ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.debate.persona.deleteCustom}
                </Button>
              ) : null}
            </div>
            <Button variant="ghost" disabled={busy} onClick={onCancel}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Panel>
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
    </Stack>
  );
}

/** The persona page of one persona or the blank form; a character (format 2) is shown read-only instead. */
export function PersonaEditor(props: {
  persona: PersonaConfig | null;
  personas: PersonaConfig[];
  teams: TeamList | null;
  teamsFailed: boolean;
  onSaved: (result: SaveResult) => void;
  onDeleted: (id: string) => void;
  onCancel: () => void;
}) {
  if (props.persona && isCharacter(props.persona)) {
    return (
      <CharacterPage
        persona={props.persona}
        teams={props.teams}
        teamsFailed={props.teamsFailed}
        onDeleted={props.onDeleted}
        onCancel={props.onCancel}
      />
    );
  }
  return <PersonaForm {...props} />;
}

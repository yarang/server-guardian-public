import { useState } from 'react';
import { Button, Icon, IconButton, SegmentedControl, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, saveTeam } from '../../api';
import { Caption, Card } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Row, Stack, mono, muted } from '../../components/layout';
import { TEAM_ID_RE, checkGlobs, memberData, normMember, normalizeTeam, parseGlobLines, riskLabel, riskTone, teamExtras } from '../../lib/debate';
import type { Member } from '../../lib/debate';
import { errorText } from '../../lib/format';
import type { PersonaConfig, SaveResult, TeamCatalog, TeamData, TeamEntry } from '../../types';
import { tr } from '../../i18n';

interface Draft {
  id: string;
  name: string;
  category: string;
  expert: Member;
  proposers: Member[];
  critics: Member[];
  judge: Member;
  rounds: string;
  ctxPaths: string;
  ctxRef: string;
  toolsMode: 'inherit' | 'explicit';
  tools: string[];
  /** Stored team keys the editor does not model (`flow`, `sides`, ...), written back on save. */
  extra: Record<string, unknown>;
}

function personaFor(personas: PersonaConfig[], kind: 'Expert' | 'Critic' | 'Decision'): string {
  const hit = personas.find((p) => (p.role || '').includes(kind));
  return (hit || personas[0])?.id || '';
}

function initialDraft(entry: TeamEntry | null, personas: PersonaConfig[]): Draft {
  if (!entry) {
    return {
      id: '',
      name: '',
      category: '',
      expert: { persona: personaFor(personas, 'Expert'), providers: [] },
      proposers: [],
      critics: [{ persona: personaFor(personas, 'Critic'), providers: [] }],
      judge: { persona: personaFor(personas, 'Decision'), providers: [] },
      rounds: '',
      ctxPaths: '',
      ctxRef: '',
      toolsMode: 'inherit',
      tools: [],
      extra: {},
    };
  }
  const d = normalizeTeam(entry.data || { id: entry.id });
  return {
    id: entry.id,
    name: d.name,
    category: d.category || '',
    expert: normMember(d.expert),
    proposers: (d.proposers || []).map(normMember),
    critics: d.critic.length ? d.critic.map(normMember) : [{ persona: personaFor(personas, 'Critic'), providers: [] }],
    judge: normMember(d.judge),
    rounds: d.max_rounds !== undefined ? String(d.max_rounds) : '',
    ctxPaths: d.context ? d.context.paths.join('\n') : '',
    ctxRef: d.context?.ref || '',
    toolsMode: d.allowed_tools !== undefined ? 'explicit' : 'inherit',
    tools: d.allowed_tools ? [...d.allowed_tools] : [],
    extra: teamExtras(entry.data || { id: entry.id }),
  };
}

/** Returns the first problem with the draft, or '' when it can be saved. */
function validate(d: Draft, isNew: boolean, catalog: TeamCatalog): string {
  if (isNew && !TEAM_ID_RE.test(d.id)) return tr.debate.teamDialog.badId;
  if (!d.expert.persona) return tr.debate.teamDialog.needExpert;
  if (!d.judge.persona) return tr.debate.teamDialog.needJudge;
  const max = catalog.max_critics || 4;
  if (d.critics.length < 1 || d.critics.length > max) return tr.debate.teamDialog.criticCount(max);
  if (d.critics.some((c) => !c.persona)) return tr.debate.teamDialog.needCritic;
  if (new Set(d.critics.map((c) => c.persona)).size !== d.critics.length) return tr.debate.teamDialog.criticDistinct;
  const maxP = catalog.max_proposers ?? 2;
  if (d.proposers.length > maxP) return tr.debate.teamDialog.proposerCount(maxP);
  if (d.proposers.some((p) => !p.persona)) return tr.debate.teamDialog.needProposer;
  if (new Set([d.expert.persona, ...d.proposers.map((p) => p.persona)]).size !== d.proposers.length + 1) {
    return tr.debate.teamDialog.proposerDistinct;
  }
  if (d.rounds.trim() !== '') {
    const n = Number(d.rounds);
    const maxR = catalog.max_rounds || 10;
    if (!Number.isInteger(n) || n < 1 || n > maxR) return tr.debate.teamDialog.badRounds(maxR);
  }
  return checkGlobs(parseGlobLines(d.ctxPaths));
}

function toPayload(d: Draft): TeamData {
  const payload: TeamData = {
    ...d.extra,
    id: d.id,
    name: d.name.trim() || d.id,
    expert: memberData(d.expert),
    critic: d.critics.map(memberData),
    judge: memberData(d.judge),
  };
  if (d.category) payload.category = d.category;
  if (d.proposers.length) payload.proposers = d.proposers.map(memberData);
  if (d.rounds.trim() !== '') payload.max_rounds = Number(d.rounds);
  if (d.toolsMode === 'explicit') payload.allowed_tools = [...d.tools];
  const paths = parseGlobLines(d.ctxPaths);
  if (paths.length) {
    payload.context = { paths };
    if (d.ctxRef.trim()) payload.context.ref = d.ctxRef.trim();
  }
  return payload;
}

// --- Member editor -----------------------------------------------------------------------------

function PersonaSelect({
  label,
  value,
  personas,
  onChange,
}: {
  label: string;
  value: string;
  personas: PersonaConfig[];
  onChange: (id: string) => void;
}) {
  const options = personas.map((p) => ({ value: p.id, label: `${p.name} (${p.id})` }));
  if (value && !personas.some((p) => p.id === value)) options.unshift({ value, label: tr.debate.teamDialog.missingPersona(value) });
  if (!value) options.unshift({ value: '', label: tr.debate.teamDialog.select });
  return <SelectField label={label} value={value} options={options} onChange={(e) => onChange(e.target.value)} />;
}

function ProviderEditor({
  who,
  providers,
  catalog,
  onChange,
}: {
  who: string;
  providers: string[];
  catalog: TeamCatalog;
  onChange: (next: string[]) => void;
}) {
  const unselected = catalog.providers.filter((p) => !providers.includes(p));
  const [choice, setChoice] = useState('');
  const selected = unselected.includes(choice) ? choice : (unselected[0] ?? '');

  const move = (i: number, delta: number) => {
    const next = [...providers];
    [next[i], next[i + delta]] = [next[i + delta], next[i]];
    onChange(next);
  };

  return (
    <Stack gap="var(--sg-s-3)">
      <Caption>{providers.length ? tr.debate.teamDialog.providerOrder : tr.debate.teamDialog.providerOrderEmpty}</Caption>
      {providers.map((p, i) => {
        const known = catalog.providers.includes(p);
        const installed = catalog.installed.includes(p);
        return (
          <Row key={p} gap="var(--sg-s-3)" style={{ flexWrap: 'nowrap' }}>
            <span style={{ ...mono, fontSize: 13 }}>{`${i + 1}. ${p}`}</span>
            {!known ? (
              <StatusChip tone="crit">{tr.debate.teamDialog.unknownProvider}</StatusChip>
            ) : !installed ? (
              <StatusChip tone="warn">{tr.debate.teamDialog.notInstalled}</StatusChip>
            ) : null}
            <span style={{ marginLeft: 'auto', display: 'flex', gap: 'var(--sg-s-1)' }}>
              <Button size="sm" variant="outline" disabled={i === 0} aria-label={tr.debate.teamDialog.moveUp(p, who)} onClick={() => move(i, -1)}>
                ↑
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={i === providers.length - 1}
                aria-label={tr.debate.teamDialog.moveDown(p, who)}
                onClick={() => move(i, 1)}
              >
                ↓
              </Button>
              <IconButton aria-label={tr.debate.teamDialog.removeProvider(p, who)} icon={<Icon name="x" />} onClick={() => onChange(providers.filter((_, j) => j !== i))} />
            </span>
          </Row>
        );
      })}
      {unselected.length ? (
        <Row style={{ alignItems: 'flex-end' }}>
          <SelectField
            label={tr.debate.teamDialog.addProvider(who)}
            value={selected}
            options={unselected.map((p) => ({ value: p, label: `${p}${catalog.installed.includes(p) ? '' : ` (${tr.debate.teamDialog.notInstalled})`}` }))}
            onChange={(e) => setChoice(e.target.value)}
          />
          <Button size="sm" variant="outline" aria-label={tr.debate.teamDialog.addProviderTo(who)} onClick={() => selected && onChange([...providers, selected])}>
            {tr.common.add}
          </Button>
        </Row>
      ) : null}
    </Stack>
  );
}

/** Where a member runs: the hub, a placement label, or one enabled worker. A stored value the hub no longer offers stays selectable, flagged. */
function HostSelect({ who, host, catalog, onChange }: { who: string; host: string | undefined; catalog: TeamCatalog; onChange: (host: string) => void }) {
  const labels = catalog.host_labels ?? [];
  const workers = catalog.workers ?? [];
  // The hub always offers the placement labels, but they mean nothing without an enabled worker: keep the field out of
  // the way unless there is a worker or a stored value to show.
  if (!workers.length && !host) return null;
  const known = [...labels, ...workers];
  const options = [
    { value: '', label: tr.debate.teamDialog.hostHub },
    ...labels.map((v) => ({ value: v, label: tr.debate.teamDialog.hostLabel(v) })),
    ...workers.map((v) => ({ value: v, label: v })),
    ...(host && !known.includes(host) ? [{ value: host, label: tr.debate.teamDialog.hostNotWorker(host) }] : []),
  ];
  return (
    <Stack gap="var(--sg-s-2)">
      <SelectField label={tr.debate.teamDialog.hostOf(who)} value={host ?? ''} options={options} onChange={(e) => onChange(e.target.value)} />
      <div style={{ ...muted, fontSize: 12 }}>{tr.debate.teamDialog.hostNote}</div>
    </Stack>
  );
}

function MemberEditor({
  who,
  member,
  personas,
  catalog,
  onChange,
}: {
  who: string;
  member: Member;
  personas: PersonaConfig[];
  catalog: TeamCatalog;
  onChange: (m: Member) => void;
}) {
  return (
    <Stack gap="var(--sg-s-4)">
      <PersonaSelect label={tr.debate.teamDialog.personaOf(who)} value={member.persona} personas={personas} onChange={(persona) => onChange({ ...member, persona })} />
      <ProviderEditor who={who} providers={member.providers} catalog={catalog} onChange={(providers) => onChange({ ...member, providers })} />
      <HostSelect who={who} host={member.host} catalog={catalog} onChange={(host) => {
        const { host: _drop, ...rest } = member;
        onChange(host ? { ...rest, host } : rest);
      }} />
      <Stack gap="var(--sg-s-2)">
        <CheckboxField
          checked={member.codeTools === 'mcp'}
          onChange={(e) => {
            const { codeTools: _drop, ...rest } = member;
            onChange(e.target.checked ? { ...rest, codeTools: 'mcp' } : rest);
          }}
        >
          {tr.debate.teamDialog.codeTools(who)}
        </CheckboxField>
        {member.codeTools === 'mcp' ? <div style={{ ...muted, fontSize: 12 }}>{tr.debate.teamDialog.mcpNote}</div> : null}
      </Stack>
    </Stack>
  );
}

// --- Tools -------------------------------------------------------------------------------------

function ToolsEditor({
  catalog,
  mode,
  tools,
  onMode,
  onTools,
}: {
  catalog: TeamCatalog;
  mode: Draft['toolsMode'];
  tools: string[];
  onMode: (m: Draft['toolsMode']) => void;
  onTools: (t: string[]) => void;
}) {
  const known = catalog.tools.map((t) => t.name);
  const rows = [
    ...catalog.tools,
    ...tools.filter((t) => !known.includes(t)).map((name) => ({ name, risk: 'unknown', description: tr.debate.teamDialog.notInCatalog })),
  ];
  const explicit = mode === 'explicit';
  const highChecked = explicit && rows.some((t) => tools.includes(t.name) && t.risk.toLowerCase() === 'high');
  return (
    <Card>
      <Caption>{tr.debate.teamDialog.allowedTools}</Caption>
      <SegmentedControl
        aria-label={tr.debate.teamDialog.toolsMode}
        value={mode}
        options={[
          { value: 'inherit', label: tr.debate.teamDialog.inherit },
          { value: 'explicit', label: tr.debate.teamDialog.restrict },
        ]}
        onChange={(v) => onMode(v as Draft['toolsMode'])}
      />
      <div style={{ ...muted, fontSize: 12 }}>
        {explicit
          ? tr.debate.teamDialog.explicitNote
          : tr.debate.teamDialog.inheritNote}{' '}
        {tr.debate.teamDialog.narrowNote}
      </div>
      {explicit ? (
        <Stack gap="var(--sg-s-3)">
          {rows.map((t) => (
            <CheckboxField
              key={t.name}
              checked={tools.includes(t.name)}
              onChange={(e) => onTools(e.target.checked ? [...tools, t.name] : tools.filter((x) => x !== t.name))}
            >
              <span style={{ ...mono, fontSize: 13 }}>{t.name}</span> <StatusChip tone={riskTone(t.risk)}>{riskLabel(t.risk)}</StatusChip>
              <span style={{ display: 'block', ...muted, fontSize: 12 }}>{t.description}</span>
            </CheckboxField>
          ))}
        </Stack>
      ) : null}
      {highChecked ? (
        <Notice tone="warn">{tr.debate.teamDialog.highRisk}</Notice>
      ) : null}
    </Card>
  );
}

// --- Dialog ------------------------------------------------------------------------------------

/** Team editor: id, name, expert / critic panel / judge with provider order, rounds, reference code, tools. */
export function TeamDialog({
  entry,
  personas,
  catalog,
  onClose,
  onSaved,
}: {
  entry: TeamEntry | null;
  personas: PersonaConfig[];
  catalog: TeamCatalog;
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
}) {
  const isNew = entry === null;
  const [draft, setDraft] = useState<Draft>(() => initialDraft(entry, personas));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p }));
  const maxCritics = catalog.max_critics || 4;
  const maxProposers = catalog.max_proposers ?? 2;
  const maxRounds = catalog.max_rounds || 10;

  const subtitle = isNew
    ? tr.debate.teamDialog.subNew
    : entry.source === 'builtin'
      ? tr.debate.teamDialog.subBuiltin
      : tr.debate.teamDialog.subCustom;

  const setCritic = (i: number, m: Member) => patch({ critics: draft.critics.map((c, j) => (j === i ? m : c)) });
  const addCritic = () => {
    if (draft.critics.length >= maxCritics) return;
    const used = draft.critics.map((c) => c.persona);
    const next = personas.find((p) => (p.role || '').includes('Critic') && !used.includes(p.id)) || personas.find((p) => !used.includes(p.id));
    patch({ critics: [...draft.critics, { persona: next ? next.id : '', providers: [] }] });
  };

  const setProposer = (i: number, m: Member) => patch({ proposers: draft.proposers.map((p, j) => (j === i ? m : p)) });
  const addProposer = () => {
    if (draft.proposers.length >= maxProposers) return;
    const used = [draft.expert.persona, ...draft.proposers.map((p) => p.persona)];
    const next = personas.find((p) => (p.role || '').includes('Expert') && !used.includes(p.id)) || personas.find((p) => !used.includes(p.id));
    patch({ proposers: [...draft.proposers, { persona: next ? next.id : '', providers: [] }] });
  };

  const save = async () => {
    const problem = validate(draft, isNew, catalog);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError('');
    try {
      onSaved(await saveTeam(toPayload(draft)));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal title={isNew ? tr.debate.teamDialog.newTitle : tr.debate.teamDialog.editTitle(draft.id)} width={780} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{subtitle}</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)' }}>
          <TextField
            label={tr.debate.teamDialog.id}
            mono
            value={draft.id}
            readOnly={!isNew}
            placeholder={tr.debate.teamDialog.idPlaceholder}
            onChange={(e) => patch({ id: e.target.value.trim() })}
          />
          <TextField label={tr.debate.persona.name} value={draft.name} onChange={(e) => patch({ name: e.target.value })} />
          <SelectField
            label={tr.debate.teamDialog.category}
            value={draft.category}
            options={[
              { value: '', label: tr.debate.teamDialog.categoryNone },
              ...(catalog.categories || []).map((c) => ({ value: c, label: tr.debate.team.category[c] ?? c })),
              ...(draft.category && !(catalog.categories || []).includes(draft.category) ? [{ value: draft.category, label: draft.category }] : []),
            ]}
            onChange={(e) => patch({ category: e.target.value })}
          />
        </div>

        <Card>
          <Caption>{tr.debate.teamDialog.expertSingle}</Caption>
          <MemberEditor who={tr.debate.team.expert} member={draft.expert} personas={personas} catalog={catalog} onChange={(m) => patch({ expert: m })} />
        </Card>

        <Card>
          <Row style={{ justifyContent: 'space-between' }}>
            <Caption>{tr.debate.teamDialog.proposers(draft.proposers.length, maxProposers)}</Caption>
            <Button size="sm" variant="outline" disabled={draft.proposers.length >= maxProposers} onClick={addProposer}>
              {tr.debate.teamDialog.addProposer}
            </Button>
          </Row>
          <div style={{ ...muted, fontSize: 12 }}>{tr.debate.teamDialog.proposersNote}</div>
          <Stack gap="var(--sg-s-4)">
            {draft.proposers.map((p, i) => (
              <Card key={i} style={{ background: 'var(--sg-surface)' }}>
                <MemberEditor who={tr.debate.team.proposerN(i + 1)} member={p} personas={personas} catalog={catalog} onChange={(m) => setProposer(i, m)} />
                <div>
                  <Button
                    size="sm"
                    variant="danger"
                    aria-label={tr.debate.teamDialog.removeProposerAria(i + 1)}
                    onClick={() => patch({ proposers: draft.proposers.filter((_, j) => j !== i) })}
                  >
                    {tr.debate.teamDialog.removeProposer}
                  </Button>
                </div>
              </Card>
            ))}
          </Stack>
        </Card>

        <Card>
          <Row style={{ justifyContent: 'space-between' }}>
            <Caption>{tr.debate.teamDialog.criticPanel(draft.critics.length, maxCritics)}</Caption>
            <Button size="sm" variant="outline" disabled={draft.critics.length >= maxCritics} onClick={addCritic}>
              {tr.debate.teamDialog.addCritic}
            </Button>
          </Row>
          <Stack gap="var(--sg-s-4)">
            {draft.critics.map((c, i) => (
              <Card key={i} style={{ background: 'var(--sg-surface)' }}>
                <MemberEditor who={tr.debate.team.criticN(i + 1)} member={c} personas={personas} catalog={catalog} onChange={(m) => setCritic(i, m)} />
                <div>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={draft.critics.length <= 1}
                    aria-label={tr.debate.teamDialog.removeCriticAria(i + 1)}
                    onClick={() => patch({ critics: draft.critics.filter((_, j) => j !== i) })}
                  >
                    {tr.debate.teamDialog.removeCritic}
                  </Button>
                </div>
              </Card>
            ))}
          </Stack>
        </Card>

        <Card>
          <Caption>{tr.debate.teamDialog.judgeSingle}</Caption>
          <MemberEditor who={tr.debate.team.judge} member={draft.judge} personas={personas} catalog={catalog} onChange={(m) => patch({ judge: m })} />
        </Card>

        <TextField
          label={tr.debate.teamDialog.maxRounds(maxRounds)}
          type="number"
          min={1}
          max={maxRounds}
          step={1}
          value={draft.rounds}
          onChange={(e) => patch({ rounds: e.target.value })}
        />

        <Stack gap="var(--sg-s-4)">
          <TextAreaField
            label={tr.debate.teamDialog.ctxPaths}
            mono
            rows={3}
            placeholder="apps/agent/*.go"
            value={draft.ctxPaths}
            onChange={(e) => patch({ ctxPaths: e.target.value })}
          />
          <TextField label={tr.debate.teamDialog.ctxRef} mono placeholder="HEAD" value={draft.ctxRef} onChange={(e) => patch({ ctxRef: e.target.value })} />
        </Stack>

        <ToolsEditor
          catalog={catalog}
          mode={draft.toolsMode}
          tools={draft.tools}
          onMode={(m) => patch({ toolsMode: m })}
          onTools={(t) => patch({ tools: t })}
        />

        {error ? <Notice tone="crit">{error}</Notice> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sg-s-4)' }}>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            {tr.common.cancel}
          </Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            {!isNew && entry.source === 'builtin' ? tr.debate.teamDialog.saveCustom : tr.debate.teamDialog.saveTeam}
          </Button>
        </div>
      </Stack>
    </Modal>
  );
}

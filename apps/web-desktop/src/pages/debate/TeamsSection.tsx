import { useState } from 'react';
import { Button, Panel, SegmentedControl, StatusChip } from '@server-guardian/ui';
import { deleteTeam, AuthRequiredError } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { Notice, NoticeList } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { groupTeams, normalizeTeam, normMember, sortTeams, teamCreated } from '../../lib/debate';
import type { Member, TeamSort } from '../../lib/debate';
import { LOCALE, errorText } from '../../lib/format';
import type { PersonaConfig, TeamEntry, TeamList } from '../../types';
import { SourceChip } from './SourceChip';
import { TeamDialog } from './TeamDialog';
import { tr } from '../../i18n';

/** Local calendar date of an epoch time, in the dashboard language. */
function fmtDate(ms: number): string {
  return new Date(ms).toLocaleDateString(LOCALE);
}

function MemberLine({ label, member }: { label: string; member: Member }) {
  return (
    <div>
      {`${label}: `}
      <span style={{ color: 'var(--sg-text)' }}>{member.persona || '?'}</span>{' '}
      <span style={muted}>{`[${member.providers.length ? member.providers.join(' → ') : tr.debate.team.hostDefault}]`}</span>
      {member.host ? <span style={muted}>{` ${tr.debate.team.runsOn(member.host)}`}</span> : null}
    </div>
  );
}

function TeamDetails({ team }: { team: TeamEntry }) {
  const n = normalizeTeam(team.data || { id: team.id });
  const tools = n.allowed_tools ? (n.allowed_tools.length ? n.allowed_tools.join(', ') : tr.common.none) : tr.debate.team.personaTools;
  return (
    <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.7, overflowWrap: 'anywhere' }}>
      <MemberLine label={tr.debate.team.expert} member={n.expert} />
      {(n.proposers || []).map((p, i) => <MemberLine key={`p${i}`} label={tr.debate.team.proposerN(i + 1)} member={normMember(p)} />)}
      {n.critic.length ? (
        n.critic.map((c, i) => <MemberLine key={i} label={tr.debate.team.criticN(i + 1)} member={c} />)
      ) : (
        <div>
          {`${tr.debate.team.critic}: `}<span style={{ color: 'var(--sg-crit)' }}>{tr.common.none}</span>
        </div>
      )}
      <MemberLine label={tr.debate.team.judge} member={n.judge} />
      {n.context ? (
        <div title={n.context.paths.join('\n')}>
          {tr.debate.team.codePaths(n.context.paths.length)}
          {n.context.ref ? <span style={mono}>{` @ ${n.context.ref}`}</span> : null}
        </div>
      ) : null}
      <div>{tr.debate.team.roundsTools(n.max_rounds ?? tr.debate.team.defaultWord, tools)}</div>
    </div>
  );
}

/** One list row: a summary line that opens the lineup, with the actions always in reach. */
function TeamRow({ team, onEdit, onDelete }: { team: TeamEntry; onEdit: () => void; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const n = normalizeTeam(team.data || { id: team.id });
  const errors = team.errors || [];
  const warnings = team.warnings || [];
  const created = teamCreated(team);
  return (
    <div
      style={{
        padding: 'var(--sg-s-4) var(--sg-s-5)',
        borderBottom: '1px solid var(--sg-border-subtle)',
        borderLeft: `3px solid ${errors.length ? 'var(--sg-crit)' : warnings.length ? 'var(--sg-warn)' : 'transparent'}`,
      }}
    >
      <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? tr.debate.team.collapseAria(team.id) : tr.debate.team.expandAria(team.id)}
          onClick={() => setOpen((v) => !v)}
          style={{
            all: 'unset',
            cursor: 'pointer',
            display: 'grid',
            gap: 2,
            minWidth: 0,
            flex: '1 1 320px',
          }}
        >
          <span style={{ display: 'flex', gap: 'var(--sg-s-3)', alignItems: 'center', flexWrap: 'wrap' }}>
            <span aria-hidden="true" style={{ ...muted, width: 12 }}>{open ? '▾' : '▸'}</span>
            <strong style={{ overflowWrap: 'anywhere' }}>{team.name || team.id}</strong>
            <SourceChip source={team.source} />
            {errors.length ? <StatusChip tone="crit">{tr.debate.team.errorsChip(errors.length)}</StatusChip> : null}
            {warnings.length ? <StatusChip tone="warn">{tr.debate.team.warningsChip(warnings.length)}</StatusChip> : null}
          </span>
          <span style={{ ...muted, fontSize: 12, paddingLeft: 24, overflowWrap: 'anywhere' }}>
            <span style={mono}>{team.id}</span>
            {` · ${tr.debate.team.summary(n.expert.persona || '?', n.critic.length, (n.proposers || []).length)}`}
            {` · ${created ? tr.debate.team.created(fmtDate(created)) : tr.debate.team.noDate}`}
          </span>
        </button>
        <Row style={{ flexWrap: 'nowrap' }}>
          <Button size="sm" variant="outline" aria-label={tr.debate.team.editAria(team.id)} onClick={onEdit}>
            {tr.common.edit}
          </Button>
          {team.source === 'custom' ? (
            <Button size="sm" variant="danger" aria-label={tr.debate.team.deleteAria(team.id)} onClick={onDelete}>
              {tr.common.delete}
            </Button>
          ) : null}
        </Row>
      </Row>
      {open ? (
        <div style={{ paddingLeft: 24, paddingTop: 'var(--sg-s-3)', display: 'grid', gap: 'var(--sg-s-3)' }}>
          <TeamDetails team={team} />
        </div>
      ) : null}
      <div style={{ paddingLeft: 24 }}>
        <NoticeList tone="crit" items={errors} />
        <NoticeList tone="warn" items={warnings} />
      </div>
    </div>
  );
}

function TeamList({ teams, grouped, onEdit, onDelete }: { teams: TeamEntry[]; grouped: boolean; onEdit: (t: TeamEntry) => void; onDelete: (t: TeamEntry) => void }) {
  const rows = (list: TeamEntry[]) => (
    <div style={{ border: '1px solid var(--sg-border-subtle)', borderRadius: 'var(--sg-radius-md, 8px)' }}>
      {list.map((t) => (
        <TeamRow key={t.id} team={t} onEdit={() => onEdit(t)} onDelete={() => onDelete(t)} />
      ))}
    </div>
  );
  if (!grouped) return rows(teams);
  return (
    <Stack gap="var(--sg-s-6)">
      {groupTeams(teams).map((g) => (
        <section key={g.category} aria-label={tr.debate.team.category[g.category]}>
          <h3 style={{ margin: '0 0 var(--sg-s-3)', fontSize: 13, color: 'var(--sg-text-secondary)', fontWeight: 600 }}>
            {tr.debate.team.groupCount(tr.debate.team.category[g.category], g.teams.length)}
          </h3>
          {rows(g.teams)}
        </section>
      ))}
    </Stack>
  );
}

/** Debate teams: rows grouped by goal and sorted newest first (lineup on demand), editor dialog, custom-team delete. */
export function TeamsSection({
  state,
  personas,
  onChanged,
}: {
  state: HubDataState<TeamList>;
  personas: PersonaConfig[];
  onChanged: () => void;
}) {
  const [editor, setEditor] = useState<{ entry: TeamEntry | null } | null>(null);
  const [deleting, setDeleting] = useState<TeamEntry | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ text: string; warnings: string[] } | null>(null);
  const [sortBy, setSortBy] = useState<TeamSort>('created');
  const [grouped, setGrouped] = useState(true);
  const count = state.data?.teams?.length;

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteTeam(deleting.id);
      setBanner({ text: tr.debate.team.deleted(deleting.id), warnings: [] });
      setDeleting(null);
      onChanged();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(tr.debate.team.deleteFailed(errorText(err)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title={count !== undefined ? tr.debate.team.sectionCount(count) : tr.debate.team.section}
      actions={
        <Button size="sm" variant="secondary" disabled={state.data === null || personas.length === 0} onClick={() => setEditor({ entry: null })}>
          {tr.debate.team.newButton}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-6)">
        {banner ? (
          <Stack gap="var(--sg-s-3)">
            <Notice tone="ok">{banner.text}</Notice>
            <NoticeList tone="warn" items={banner.warnings} />
          </Stack>
        ) : null}
        <DataGate state={state} what={tr.debate.team.what}>
          {(data) => (
            <Stack gap="var(--sg-s-5)">
              <Row gap="var(--sg-s-5)">
                <SegmentedControl
                  aria-label={tr.debate.team.sortAria}
                  value={sortBy}
                  options={[
                    { value: 'created', label: tr.debate.team.sortCreated },
                    { value: 'modified', label: tr.debate.team.sortModified },
                    { value: 'name', label: tr.debate.team.sortName },
                  ]}
                  onChange={(v) => setSortBy(v as TeamSort)}
                />
                <SegmentedControl
                  aria-label={tr.debate.team.viewAria}
                  value={grouped ? 'grouped' : 'flat'}
                  options={[
                    { value: 'grouped', label: tr.debate.team.viewGrouped },
                    { value: 'flat', label: tr.debate.team.viewFlat },
                  ]}
                  onChange={(v) => setGrouped(v === 'grouped')}
                />
              </Row>
              <TeamList teams={sortTeams(data.teams || [], sortBy)} grouped={grouped} onEdit={(t) => setEditor({ entry: t })} onDelete={setDeleting} />
            </Stack>
          )}
        </DataGate>
      </Stack>
      {editor && state.data ? (
        <TeamDialog
          entry={editor.entry}
          personas={personas}
          catalog={state.data.catalog}
          onClose={() => setEditor(null)}
          onSaved={(res) => {
            setBanner({ text: tr.debate.team.saved(res.id), warnings: res.warnings });
            setEditor(null);
            onChanged();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmModal
          title={tr.debate.team.deleteCustom}
          message={tr.debate.team.deleteConfirm(deleting.id)}
          confirmLabel={tr.debate.team.deleteAction}
          busy={busy}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onCancel={() => {
            setDeleting(null);
            setDeleteError(null);
          }}
        />
      ) : null}
    </Panel>
  );
}

import { useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, syncProject } from '../../api';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import { isOpsProject } from '../../lib/project';
import type { ProjectEntry, ProjectList } from '../../types';
import { ProjectDialog } from './ProjectDialog';
import { SourceChip } from './SourceChip';
import { WorkspaceResetDialog } from './WorkspaceResetDialog';
import { tr } from '../../i18n';

/** Body of an ops project's card: what it targets, how many nodes that is now, and targets that no longer exist. */
function OpsTargets({ p }: { p: ProjectEntry }) {
  const t = p.data.targets ?? {};
  const s = p.targets;
  const missing = s ? [...s.missing.groups, ...s.missing.nodes] : [];
  return (
    <>
      <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
        {tr.debate.project.opsDefinition(Boolean(t.all), t.groups ?? [], t.nodes ?? [], t.exclude ?? [])}
      </div>
      {s ? <div style={{ ...mono, fontSize: 12 }}>{tr.debate.project.opsTargets(s.ids.length, s.include_hub)}</div> : null}
      {s?.error ? <Notice tone="warn">{tr.debate.project.opsUnresolved(s.error)}</Notice> : null}
      {missing.length ? <Notice tone="warn">{tr.debate.project.opsMissing(missing)}</Notice> : null}
    </>
  );
}

function workspaceLine(p: ProjectEntry): string {
  const st = p.status;
  if (!st) return tr.debate.project.invalid;
  if (!st.present) return `${st.workspace} · ${tr.debate.project.notCloned}`;
  const dirty = st.modified ? ` · ${tr.debate.project.modified(st.modified)}` : '';
  return `${st.branch ?? '?'} @ ${(st.head ?? '').slice(0, 12)}${dirty}`;
}

/**
 * Projects the debate engine can read and change: create, edit, delete, clone / sync. Which one is
 * in use is chosen with the project switcher (a deleted selection falls back to the default there).
 */
export function ProjectsSection({ state, onChanged }: { state: HubDataState<ProjectList>; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ entry: ProjectEntry | null; isNew: boolean } | null>(null);
  const [banner, setBanner] = useState<{ tone: 'ok' | 'warn' | 'crit'; text: string; resetId?: string } | null>(null);
  const [resetting, setResetting] = useState<string | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);
  const count = state.data?.projects?.length;

  const sync = async (id: string) => {
    setSyncing(id);
    setBanner(null);
    try {
      const r = await syncProject(id);
      if (r.action === 'skipped') setBanner({ tone: 'warn', text: tr.debate.project.notUpdated(id, r.reason ?? tr.debate.project.skipped), resetId: id });
      else setBanner({ tone: 'ok', text: tr.debate.project.synced(id, tr.debate.project.syncAction[r.action] ?? r.action, r.branch ?? '', (r.head ?? '').slice(0, 12)) });
      onChanged();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setBanner({ tone: 'crit', text: `${id}: ${errorText(err)}` });
    } finally {
      setSyncing(null);
    }
  };

  return (
    <Panel
      title={count !== undefined ? tr.debate.project.sectionCount(count) : tr.debate.project.section}
      actions={
        <Button size="sm" variant="secondary" onClick={() => setEditing({ entry: null, isNew: true })}>
          {tr.debate.project.newButton}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-6)">
        {banner ? (
          <Notice tone={banner.tone}>
            {banner.text}
            {banner.resetId ? (
              <>
                {' '}
                <Button size="sm" variant="outline" aria-label={tr.debate.project.resetFromSync(banner.resetId)} onClick={() => setResetting(banner.resetId ?? null)}>
                  {tr.debate.project.resetWorkspace}
                </Button>
              </>
            ) : null}
          </Notice>
        ) : null}
        <DataGate state={state} what={tr.debate.project.what}>
          {(data) => (
            <Grid min={280} gap="var(--sg-s-6)">
              {(data.projects || []).map((p) => {
                const ops = isOpsProject(p);
                return (
                  <Card key={p.id}>
                    <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
                      <strong style={{ overflowWrap: 'anywhere' }}>{p.data.name || p.id}</strong>
                      <Row gap="var(--sg-s-2)">
                        {ops ? <StatusChip tone="info">{tr.debate.project.opsChip}</StatusChip> : null}
                        <SourceChip source={p.source} />
                      </Row>
                    </Row>
                    {p.data.description ? <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{p.data.description}</div> : null}
                    {ops ? (
                      <OpsTargets p={p} />
                    ) : (
                      <>
                        <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{p.data.repo ?? p.data.workspace}</div>
                        <div style={{ ...mono, fontSize: 12 }}>{workspaceLine(p)}</div>
                      </>
                    )}
                    {p.errors.map((e) => (
                      <Notice key={e} tone="crit">
                        {e}
                      </Notice>
                    ))}
                    <Row gap="var(--sg-s-3)">
                      {ops ? null : (
                      <Button
                        size="sm"
                        variant="outline"
                        loading={syncing === p.id}
                        disabled={syncing !== null || p.errors.length > 0}
                        aria-label={tr.debate.project.syncAria(p.id)}
                        onClick={() => void sync(p.id)}
                      >
                        {p.status?.present ? tr.debate.project.sync : tr.debate.project.clone}
                      </Button>
                      )}
                      {!ops && p.status?.present ? (
                        <Button size="sm" variant="ghost" aria-label={tr.debate.project.resetAria(p.id)} onClick={() => setResetting(p.id)}>
                          {tr.debate.project.resetWorkspace}
                        </Button>
                      ) : null}
                      <Button size="sm" variant="ghost" aria-label={tr.debate.project.editAria(p.id)} onClick={() => setEditing({ entry: p, isNew: false })}>
                        {tr.common.edit}
                      </Button>
                    </Row>
                  </Card>
                );
              })}
            </Grid>
          )}
        </DataGate>
      </Stack>
      {resetting ? (
        <WorkspaceResetDialog
          projectId={resetting}
          onClose={() => setResetting(null)}
          onDone={() => {
            setBanner(null);
            onChanged();
          }}
        />
      ) : null}
      {editing ? (
        <ProjectDialog
          entry={editing.entry}
          isNew={editing.isNew}
          onClose={() => setEditing(null)}
          onSaved={(res) => {
            setBanner({ tone: 'ok', text: tr.debate.project.saved(res.id) });
            setEditing(null);
            onChanged();
          }}
          onDeleted={(id) => {
            setBanner({ tone: 'ok', text: tr.debate.project.deleted(id) });
            setEditing(null);
            onChanged();
          }}
        />
      ) : null}
    </Panel>
  );
}

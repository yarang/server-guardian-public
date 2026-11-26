import { useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, deleteFlow, fetchFlows, fetchPersonas, fetchTeams } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { Notice, NoticeList } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import { CLOSE, START, unitOf } from '../../lib/flowGraph';
import type { FlowData, FlowEntry } from '../../types';
import { FlowEditor } from './FlowEditor';
import { tr } from '../../i18n';

const t = () => tr.flows;
const CATEGORIES = ['development', 'operations', 'design-docs', 'general'] as const;

/** One line of the shape: the jobs of each step from the start to the close, a bundle as "job ×n". */
export function shapeLine(flow: FlowData): string {
  const steps: string[] = [];
  const seen = new Set<string>();
  let frontier = [START];
  for (let guard = 0; guard < 12 && frontier.length; guard++) {
    const next = flow.links.filter((l) => frontier.includes(l.from) && !l.look_back && !l.exit).map((l) => unitOf(flow, l.to)).filter((u) => u !== CLOSE && !seen.has(u));
    const units = [...new Set(next)];
    if (!units.length) break;
    units.forEach((u) => seen.add(u));
    steps.push(units.map((u) => {
      const bundle = (flow.bundles ?? []).find((b) => b.id === u);
      const jobs = bundle ? bundle.seats.map((s) => flow.seats.find((x) => x.id === s)?.job ?? 'write') : [flow.seats.find((s) => s.id === u)?.job ?? 'write'];
      const job = t().job[jobs[0]] ?? jobs[0];
      return jobs.length > 1 ? `${job} ×${jobs.length}` : job;
    }).join(' + '));
    frontier = units;
  }
  return [...steps, t().fixed.close].join(' → ');
}

function FlowRow({ entry, onEdit, onDelete }: { entry: FlowEntry; onEdit: () => void; onDelete: () => void }) {
  const errors = entry.errors ?? [];
  const warnings = entry.warnings ?? [];
  const custom = entry.source === 'custom';
  const params = Object.keys(entry.data.params ?? {}).length;
  return (
    <div
      style={{
        padding: 'var(--sg-s-4) var(--sg-s-5)',
        borderBottom: '1px solid var(--sg-border-subtle)',
        borderLeft: `3px solid ${errors.length ? 'var(--sg-crit)' : warnings.length ? 'var(--sg-warn)' : 'transparent'}`,
      }}
    >
      <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
        <div style={{ display: 'grid', gap: 2, minWidth: 0, flex: '1 1 320px' }}>
          <span style={{ display: 'flex', gap: 'var(--sg-s-3)', alignItems: 'center', flexWrap: 'wrap' }}>
            <strong style={{ overflowWrap: 'anywhere' }}>{entry.name || entry.id}</strong>
            <StatusChip tone={custom ? 'info' : 'idle'} title={entry.source}>{t().source[entry.source] ?? entry.source}</StatusChip>
            {errors.length ? <StatusChip tone="crit">{t().summary.errorsChip(errors.length)}</StatusChip> : null}
            {warnings.length ? <StatusChip tone="warn">{t().summary.warningsChip(warnings.length)}</StatusChip> : null}
            {!errors.length && !entry.runnable ? <StatusChip tone="warn" title={entry.run_error ?? ''}>{t().summary.notRunnable}</StatusChip> : null}
          </span>
          <span style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
            <span style={mono}>{entry.id}</span>
            {` · ${shapeLine(entry.data)}`}
            {` · ${t().summary.seats(entry.data.seats.length)}`}
            {entry.calls ? ` · ${t().summary.calls(entry.calls)}` : ''}
            {params ? ` · ${t().summary.params(params)}` : ''}
          </span>
        </div>
        <Row style={{ flexWrap: 'nowrap' }}>
          <Button size="sm" variant="outline" aria-label={custom ? t().editAria(entry.id) : t().copyAria(entry.id)} onClick={onEdit}>
            {custom ? tr.common.edit : t().copyButton}
          </Button>
          {custom ? (
            <Button size="sm" variant="danger" aria-label={t().deleteAria(entry.id)} onClick={onDelete}>
              {tr.common.delete}
            </Button>
          ) : null}
        </Row>
      </Row>
      <NoticeList tone="crit" items={errors} />
      <NoticeList tone="warn" items={[...warnings, ...(entry.run_error && !errors.length ? [entry.run_error] : [])]} />
    </div>
  );
}

/** Flow kinds: every way to start work as a flow (stored flows, templates and teams), grouped by goal, and the editor. */
export function FlowsTab() {
  const flows = useHubData(fetchFlows);
  const personas = useHubData(fetchPersonas);
  const teams = useHubData(fetchTeams);
  const [editor, setEditor] = useState<{ entry: FlowEntry | null } | null>(null);
  const [deleting, setDeleting] = useState<FlowEntry | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ text: string; warnings: string[] } | null>(null);

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteFlow(deleting.id);
      setBanner({ text: t().deleted(deleting.id), warnings: [] });
      setDeleting(null);
      flows.reload();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(t().deleteFailed(errorText(err)));
    } finally {
      setBusy(false);
    }
  };

  const groups = (list: FlowEntry[]) => {
    const known = new Set<string>(CATEGORIES);
    const by = (c: string) => list.filter((e) => (known.has(e.data.category ?? '') ? e.data.category === c : c === 'general'));
    return CATEGORIES.map((c) => ({ category: c, entries: by(c).sort((a, b) => a.id.localeCompare(b.id)) })).filter((g) => g.entries.length > 0);
  };

  return (
    <Panel
      title={t().tab}
      actions={
        <Button size="sm" variant="secondary" disabled={flows.data === null} onClick={() => setEditor({ entry: null })}>
          {t().newButton}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-6)">
        <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{t().intro}</div>
        {banner ? (
          <Stack gap="var(--sg-s-3)">
            <Notice tone="ok">{banner.text}</Notice>
            <NoticeList tone="warn" items={banner.warnings} />
          </Stack>
        ) : null}
        <DataGate state={flows} what={t().what}>
          {(data) =>
            data.flows.length === 0 ? (
              <div style={muted}>{t().empty}</div>
            ) : (
              <Stack gap="var(--sg-s-6)">
                {groups(data.flows).map((g) => (
                  <section key={g.category} aria-label={t().category[g.category]}>
                    <h3 style={{ margin: '0 0 var(--sg-s-3)', fontSize: 13, color: 'var(--sg-text-secondary)', fontWeight: 600 }}>
                      {t().groupCount(t().category[g.category], g.entries.length)}
                    </h3>
                    <div style={{ border: '1px solid var(--sg-border-subtle)', borderRadius: 'var(--sg-r-md)' }}>
                      {g.entries.map((e) => (
                        <FlowRow key={e.id} entry={e} onEdit={() => setEditor({ entry: e })} onDelete={() => setDeleting(e)} />
                      ))}
                    </div>
                  </section>
                ))}
              </Stack>
            )
          }
        </DataGate>
      </Stack>
      {editor && flows.data ? (
        <FlowEditor
          entry={editor.entry}
          personas={personas.data?.personas ?? []}
          catalog={teams.data?.catalog ?? null}
          onClose={() => setEditor(null)}
          onSaved={(res) => {
            setBanner({ text: t().saved(res.id), warnings: res.warnings });
            setEditor(null);
            flows.reload();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmModal
          title={t().deleteTitle}
          message={t().deleteConfirm(deleting.id)}
          confirmLabel={t().deleteAction}
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

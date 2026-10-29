import { useMemo, useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { Card } from '../../components/Card';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Ago } from '../../components/Ago';
import { Row, Stack, muted } from '../../components/layout';
import { useNow } from '../../hooks/useNow';
import type { HubDataState } from '../../hooks/useHubData';
import { elapsedText, jobStatusTone, parseTime } from '../../lib/debate';
import { attentionPages, staleCount, wikiJobs } from '../../lib/wiki';
import { hrefFor, navigate } from '../../router';
import type { DebateJob, WikiBuildRequest, WikiList } from '../../types';
import { PageLine } from './WikiIndex';
import { WikiEditDialog } from './WikiEditDialog';

type Confirm = 'all' | 'features' | 'groups' | null;

/** The build jobs of the project, newest first, with the page list's own state beside them. */
function JobTable({ jobs, now }: { jobs: DebateJob[]; now: number }) {
  const columns: Column<DebateJob>[] = [
    { header: tr.wiki.history.task, render: (j) => <span style={{ overflowWrap: 'anywhere' }}>{j.task || tr.wiki.jobLabel}</span> },
    {
      header: tr.wiki.history.status,
      render: (j) => (
        <StatusChip tone={jobStatusTone(j.status)} title={j.status}>
          {tr.debate.jobStatus[j.status] ?? j.status}
        </StatusChip>
      ),
    },
    {
      header: tr.wiki.history.started,
      render: (j) => {
        const at = parseTime(j.started_at);
        return at === null ? '-' : <Ago at={at} />;
      },
    },
    { header: tr.wiki.history.duration, render: (j) => elapsedText(j, now), mono: true },
    {
      header: '',
      render: (j) => (
        <a href={hrefFor('debates', [j.id])} aria-label={tr.wiki.history.followAria(j.id)}>
          {tr.wiki.history.follow}
        </a>
      ),
    },
  ];
  return <DataTable columns={columns} rows={jobs} getKey={(j) => j.id} label={tr.wiki.history.tableLabel} empty={tr.wiki.history.empty} />;
}

/**
 * History mode of the wiki tab (`?view=history`): every action that starts a build or adds a page, the pages that need
 * a check, and the build jobs of the project. Reading and file browsing carry none of these buttons.
 */
export function WikiHistory({
  project,
  list,
  jobs,
  busy,
  onBuild,
}: {
  project: string;
  list: HubDataState<WikiList>;
  jobs: readonly DebateJob[] | null;
  busy: boolean;
  onBuild: (req: WikiBuildRequest) => void;
}) {
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [creating, setCreating] = useState(false);
  const rows = useMemo(() => wikiJobs(jobs, project), [jobs, project]);
  const now = useNow(rows.some((j) => j.status === 'running'));
  const reloadList = list.reload;
  const pages = list.data?.pages;
  const attention = useMemo(() => attentionPages(pages ?? []), [pages]);

  const start = (req: WikiBuildRequest) => {
    setConfirm(null);
    onBuild(req);
  };

  return (
    <Stack>
      <Card>
        <Stack gap="var(--sg-s-4)">
          <strong>{tr.wiki.history.actionsTitle}</strong>
          <Row>
            <Button size="sm" variant="outline" loading={list.loading} onClick={reloadList}>
              {tr.wiki.tab.reload}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setCreating(true)}>
              {tr.wiki.tab.newPage}
            </Button>
            {pages && pages.length === 0 ? (
              <Button size="sm" variant="primary" disabled={busy} onClick={() => start({})}>
                {tr.wiki.tab.writeFirst}
              </Button>
            ) : null}
            {pages && pages.length > 0 ? (
              <>
                <Button size="sm" variant="primary" disabled={busy || staleCount(pages) === 0} onClick={() => start({ stale: true })}>
                  {tr.wiki.tab.rewriteStale(staleCount(pages))}
                </Button>
                <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirm('all')}>
                  {tr.wiki.tab.rewriteAll}
                </Button>
                <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirm('features')}>
                  {tr.wiki.tab.writeFeatures}
                </Button>
                <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirm('groups')}>
                  {tr.wiki.tab.writeGroups}
                </Button>
              </>
            ) : null}
          </Row>
        </Stack>
      </Card>

      <Card>
        <Stack gap="var(--sg-s-4)">
          <strong>{tr.wiki.history.title}</strong>
          <JobTable jobs={rows} now={now} />
        </Stack>
      </Card>

      <DataGate state={list} what={tr.wiki.tab.pagesWhat}>
        {() => (
          <Card>
            <Stack gap="var(--sg-s-4)">
              <strong>{tr.wiki.history.pagesTitle}</strong>
              {attention.length === 0 ? <div style={{ ...muted, fontSize: 13 }}>{tr.wiki.history.pagesNone}</div> : null}
              {attention.map((p) => (
                <PageLine key={p.id} page={p} labelled={false} />
              ))}
            </Stack>
          </Card>
        )}
      </DataGate>

      {confirm === 'all' ? (
        <ConfirmModal
          title={tr.wiki.tab.confirmAllTitle}
          message={tr.wiki.tab.confirmAllMessage}
          confirmLabel={tr.wiki.tab.confirmAllLabel}
          tone="primary"
          onConfirm={() => start({ all: true })}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm === 'groups' ? (
        <ConfirmModal
          title={tr.wiki.tab.writeGroupsTitle}
          message={tr.wiki.tab.writeGroupsMessage}
          confirmLabel={tr.wiki.tab.writeGroups}
          tone="primary"
          onConfirm={() => start({ kind: 'group' })}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {confirm === 'features' ? (
        <ConfirmModal
          title={tr.wiki.tab.writeFeaturesTitle}
          message={tr.wiki.tab.writeFeaturesMessage}
          confirmLabel={tr.wiki.tab.writeFeatures}
          tone="primary"
          onConfirm={() => start({ kind: 'feature' })}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
      {creating ? (
        <WikiEditDialog
          project={project}
          isNew
          draft={{ id: '', title: '', scope: [], body: '' }}
          onClose={() => setCreating(false)}
          onSaved={(id) => {
            setCreating(false);
            reloadList();
            navigate('debates', ['wiki', id]);
          }}
        />
      ) : null}
    </Stack>
  );
}

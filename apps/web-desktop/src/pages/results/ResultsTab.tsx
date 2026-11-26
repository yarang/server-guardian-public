import { StatusChip } from '@server-guardian/ui';
import { fetchProjectResults } from '../../api';
import { tr } from '../../i18n';
import { Ago } from '../../components/Ago';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { ProjectSwitcher } from '../../components/ProjectSwitcher';
import { Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { decisionText, decisionWordStatus, parseTime } from '../../lib/debate';
import { parseResultsRoute, resultsParams } from '../../lib/results';
import { hrefFor } from '../../router';
import { useProjectStore } from '../../state/projectStore';
import type { ResultItem } from '../../types';
import { ReportView } from '../debate/ReportButton';

const href = (thread?: string) => hrefFor('debates', resultsParams({ thread }));

const rowStyle = {
  display: 'grid',
  gap: 'var(--sg-s-2)',
  padding: 'var(--sg-s-4) var(--sg-s-5)',
  background: 'var(--sg-surface-raised)',
  color: 'var(--sg-text)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  fontSize: 13,
  textDecoration: 'none',
  minWidth: 0,
} as const;

const metaStyle = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)' } as const;

function When({ iso }: { iso: string }) {
  const at = parseTime(iso);
  return at === null ? null : (
    <span style={{ ...muted, fontSize: 12 }}>
      <Ago at={at} />
    </span>
  );
}

/** Chips and counts shared by a list row and the detail header. */
function ItemMeta({ item }: { item: ResultItem }) {
  // The raw word stays in the title; a word the dashboard does not know is shown as it is, neutral.
  const decision = decisionWordStatus(item.status);
  return (
    <span style={metaStyle}>
      {item.status ? (
        <StatusChip tone={decision?.tone ?? 'idle'} title={item.status}>
          {decision ? decisionText(decision.label) : item.status}
        </StatusChip>
      ) : null}
      {item.rounds > 0 ? <span style={{ ...muted, fontSize: 12 }}>{tr.results.rounds(item.rounds)}</span> : null}
      {item.artifact_versions > 0 ? (
        <StatusChip tone="info">{tr.results.artifactCount(item.artifact_versions, item.artifact_files)}</StatusChip>
      ) : null}
      {!item.has_report ? <StatusChip tone="idle">{tr.results.noReport}</StatusChip> : null}
      <When iso={item.updated_at} />
    </span>
  );
}

function ResultList({ project, items, total }: { project: string; items: ResultItem[]; total: number }) {
  if (items.length === 0) return <Notice tone="info">{tr.results.empty}</Notice>;
  return (
    <nav aria-label={tr.results.listAria} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
      <strong style={{ fontSize: 13 }}>{tr.results.listTitle(total)}</strong>
      {total > items.length ? <Notice tone="info">{tr.results.capped(items.length, total)}</Notice> : null}
      {items.map((item) => (
        <a key={item.thread_id} href={href(item.thread_id)} aria-label={tr.results.openThread(item.title)} style={rowStyle}>
          <span style={{ overflowWrap: 'anywhere' }}>{item.title}</span>
          <ItemMeta item={item} />
          <span style={{ ...mono, ...muted, fontSize: 11 }}>{item.thread_id}</span>
        </a>
      ))}
    </nav>
  );
}

/**
 * One result: the report inline, and a link to the Artifacts viewer when the thread has artifacts. A thread missing from the list (older than the cap) still tries the report, which answers 404
 * when there is none.
 */
function ResultDetail({
  thread,
  project,
  item,
}: {
  thread: string;
  project: string;
  item?: ResultItem;
}) {
  return (
    <Stack gap="var(--sg-s-5)">
      <div>
        <a href={href()}>{tr.results.backToList}</a>
      </div>
      <Card>
        <strong style={{ overflowWrap: 'anywhere' }}>{tr.results.detailTitle(item?.title ?? thread)}</strong>
        <span style={{ ...mono, ...muted, fontSize: 11 }}>{tr.results.threadId(thread)}</span>
        {item ? <ItemMeta item={item} /> : null}
        {item && item.artifact_versions > 0 ? (
          <div>
            <a href={hrefFor('debates', ['artifacts', thread])} title={tr.results.openArtifactsTitle(thread)}>
              {tr.results.openArtifacts}
            </a>
          </div>
        ) : null}
      </Card>
      {item && !item.has_report ? <Notice tone="info">{tr.results.reportMissing}</Notice> : <ReportView thread={thread} project={project} />}
    </Stack>
  );
}

function ResultsBody({ project, route }: { project: string; route: { thread?: string } }) {
  const state = useHubData(() => fetchProjectResults(project));
  return (
    <DataGate state={state} what={tr.results.listWhat}>
      {(list) =>
        route.thread ? (
          <ResultDetail
            key={route.thread}
            thread={route.thread}
            project={project}
            item={list.items.find((i) => i.thread_id === route.thread)}
          />
        ) : (
          <ResultList project={project} items={list.items} total={list.total} />
        )
      }
    </DataGate>
  );
}

/**
 * Results of the selected project, whatever its kind: `#/debates/results` lists the threads that left a report or
 * artifacts (newest first), `#/debates/results/<thread>` opens one. Read-only; the report and the artifacts are
 * served by the same authenticated routes as the debate's own result view.
 */
export function ResultsTab({ params }: { params: string[] }) {
  const route = parseResultsRoute(params);
  const { selected } = useProjectStore();
  return (
    <Stack>
      <ProjectSwitcher />
      {/* Keyed by project so a switch fetches that project's list instead of keeping the old one. */}
      <ResultsBody key={selected} project={selected} route={route} />
    </Stack>
  );
}

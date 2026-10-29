import { StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { DataGate } from '../../components/DataGate';
import { Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { useNow } from '../../hooks/useNow';
import { elapsedText, jobStatusTone } from '../../lib/debate';
import { WIKI_JOB_LABEL, isWikiJob } from '../../lib/wiki';
import { sortRuns } from '../../lib/agentMonitor';
import { retrievalText } from '../../lib/format';
import { hrefFor } from '../../router';
import type { DebateJob } from '../../types';

function RunRow({ job, selected, round, now }: { job: DebateJob; selected: boolean; round?: number; now: number }) {
  const running = job.status === 'running';
  const wiki = isWikiJob(job);
  const roundText = job.rounds
    ? running && round
      ? tr.agents.list.roundOf(round, job.rounds)
      : tr.agents.list.rounds(job.rounds)
    : round
      ? tr.agents.list.round(round)
      : '';
  return (
    <a
      href={hrefFor('debates', [job.id])}
      aria-current={selected ? 'true' : undefined}
      aria-label={wiki ? tr.agents.list.openWiki(job.id) : tr.agents.list.openDebate(job.id)}
      style={{
        display: 'grid',
        gap: 'var(--sg-s-2)',
        padding: 'var(--sg-s-4) var(--sg-s-5)',
        background: selected ? 'var(--sg-surface-selected)' : 'var(--sg-surface-raised)',
        color: 'var(--sg-text)',
        border: `1px solid ${selected ? 'var(--sg-info-border)' : 'var(--sg-border)'}`,
        borderRadius: 'var(--sg-r-md)',
        fontSize: 13,
        textDecoration: 'none',
        minWidth: 0,
      }}
    >
      <div
        style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', overflowWrap: 'anywhere' }}
        title={job.task}
      >
        {job.task}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)' }}>
        <StatusChip tone={jobStatusTone(job.status)} title={job.status}>
          {tr.agents.jobStatus[job.status] ?? job.status}
        </StatusChip>
        {wiki ? <StatusChip tone="info">{WIKI_JOB_LABEL}</StatusChip> : null}
        {job.thread_closed ? <StatusChip tone="idle">{tr.agents.list.closed}</StatusChip> : null}
        {job.thread_published ? <StatusChip tone="ok">{tr.agents.list.published}</StatusChip> : null}
        {job.context_paths && job.context_paths.length ? (
          <span title={job.context_paths.join('\n')}>
            <StatusChip tone="info">{tr.agents.list.code}</StatusChip>
          </span>
        ) : null}
        <span style={{ ...mono, ...muted, fontSize: 12 }}>{job.project || tr.agents.list.defaultProject}</span>
        <span style={{ ...muted, fontSize: 12 }}>{elapsedText(job, now)}</span>
        {roundText ? <span style={{ ...muted, fontSize: 12 }}>{roundText}</span> : null}
        {job.retrieval ? <span style={{ ...muted, fontSize: 12 }}>{retrievalText(job.retrieval)}</span> : null}
      </div>
    </a>
  );
}

/** Running debates first, then recent ones. */
export function RunList({
  state,
  selectedId,
  rounds,
}: {
  state: HubDataState<DebateJob[]>;
  selectedId: string | null;
  /** Current round per running job id. */
  rounds: Record<string, number>;
}) {
  const anyRunning = state.data?.some((j) => j.status === 'running') ?? false;
  const now = useNow(anyRunning);
  return (
    <DataGate state={state} what={tr.agents.list.what}>
      {(jobs) =>
        jobs.length === 0 ? (
          <div style={{ ...muted, fontSize: 13 }}>{tr.agents.list.empty}</div>
        ) : (
          <nav aria-label={tr.agents.list.aria} style={{ maxHeight: 'min(70vh, 640px)', overflowY: 'auto' }}>
            <Stack gap="var(--sg-s-3)">
              {sortRuns(jobs).map((j) => (
                <RunRow key={j.id} job={j} selected={j.id === selectedId} round={rounds[j.id]} now={now} />
              ))}
            </Stack>
          </nav>
        )
      }
    </DataGate>
  );
}

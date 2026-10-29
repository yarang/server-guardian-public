import { useEffect, useState } from 'react';
import { Button, CodeBlock, StatusChip, StatusDot } from '@server-guardian/ui';
import { AuthRequiredError, cancelDebate, fetchDebate } from '../../api';
import { CancelDebateButton } from '../../components/CancelDebateButton';
import { Caption, Card } from '../../components/Card';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { tr } from '../../i18n';
import { settledRun, useDebateEvents } from '../../hooks/useDebateEvents';
import { useHubData } from '../../hooks/useHubData';
import { useNow } from '../../hooks/useNow';
import { actionStatusTone, decisionStatus, decisionText, elapsedText, fmtDuration, jevGateTone, jobStatusTone, runDecision } from '../../lib/debate';
import { errorText, retrievalText } from '../../lib/format';
import { actionLabel, gateLabel, laneActive, runElapsedMs, runRetrieval } from '../../lib/agentMonitor';
import type { RunState } from '../../lib/agentMonitor';
import { WIKI_JOB_LABEL, isWikiJob, wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import { hrefFor } from '../../router';
import type { DebateJob, DebateJobDetail } from '../../types';
import { ArtifactsLink } from '../debate/ArtifactsLink';
import { ReportButton } from '../debate/ReportButton';
import { LaneCards } from './LaneCards';
import { Timeline } from './Timeline';

const JOB_POLL_MS = 3000;


function Outcome({ run }: { run: RunState }) {
  if (!run.verdict && !run.gate && run.actions.length === 0) return null;
  return (
    <Card>
      <Caption>{tr.agents.detail.outcome}</Caption>
      <Row>
        {run.verdict ? (
          <StatusChip tone={decisionStatus(run.verdict).tone} title={decisionStatus(run.verdict).label}>
            {decisionText(decisionStatus(run.verdict).label)}
          </StatusChip>
        ) : null}
        {run.gate ? (
          <span>
            {tr.agents.detail.jev}
            <StatusChip tone={jevGateTone(run.gate.gate)} title={run.gate.gate || undefined}>
              {gateLabel(run.gate.gate)}
            </StatusChip>
          </span>
        ) : null}
        {run.actions.map((a, i) => (
          <span key={`${a.id}-${i}`} style={{ fontSize: 12 }}>
            <span style={mono}>{a.tool || tr.agents.detail.actionFallback}</span>{' '}
            <StatusChip tone={actionStatusTone(a.status)} title={a.status || undefined}>
              {actionLabel(a.status)}
            </StatusChip>
            {a.id ? <> <a href={hrefFor('approvals', [a.id])}>{`#${a.id}`}</a></> : null}
          </span>
        ))}
      </Row>
      {run.gate?.reasons.length ? (
        <div style={{ ...muted, fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{run.gate.reasons.map((r) => `- ${r}`).join('\n')}</div>
      ) : null}
    </Card>
  );
}

/** Pages a code wiki build wrote or skipped so far. */
function WikiOutcome({ run }: { run: RunState }) {
  const wiki = run.wiki;
  if (!wiki) return null;
  const done = wiki.written.length + wiki.skipped.length;
  return (
    <Card>
      <Caption>{tr.agents.detail.pages(done, wiki.pages.length)}</Caption>
      <Row>
        {wiki.written.map((w, i) => (
          <span key={`w-${w.page}-${i}`} style={{ fontSize: 12 }}>
            <span style={mono}>{w.page}</span>{' '}
            <StatusChip tone={wikiStateTone(w.state)} title={w.state || undefined}>
              {w.state ? wikiStateLabel(w.state) : tr.agents.detail.written}
            </StatusChip>
          </span>
        ))}
        {wiki.skipped.map((w, i) => (
          <span key={`s-${w.page}-${i}`} style={{ fontSize: 12 }} title={w.reason}>
            <span style={mono}>{w.page}</span> <StatusChip tone="warn">{tr.agents.detail.skipped}</StatusChip>
          </span>
        ))}
        {wiki.pages
          .filter((p) => !wiki.written.some((w) => w.page === p) && !wiki.skipped.some((w) => w.page === p))
          .map((p) => (
            <span key={`p-${p}`} style={{ fontSize: 12 }}>
              <span style={mono}>{p}</span> <StatusChip tone="idle">{wiki.finished ? tr.agents.detail.notRun : tr.agents.detail.pending}</StatusChip>
            </span>
          ))}
      </Row>
    </Card>
  );
}

/** Detail of one run: header, lanes, outcome and timeline, or the old log when no events were recorded. */
export function RunDetail({ id, listJob, onChanged }: { id: string; listJob: DebateJob | null; onChanged: () => void }) {
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState('');
  // Poll the job only while it runs; starts from what the list row says.
  const [pollOn, setPollOn] = useState(listJob?.status === 'running');
  const detail = useHubData<DebateJobDetail>(() => fetchDebate(id), { pollMs: pollOn ? JOB_POLL_MS : undefined });
  const job = detail.data ?? listJob;
  const status = job?.status ?? 'running';
  const running = job ? status === 'running' : false;
  useEffect(() => setPollOn(running), [running]);

  // A finished job that never wrote events is not followed at all.
  const noEventFile = !running && listJob?.events === false;
  // Wait until the job is known: the list row says whether an event file exists.
  const events = useDebateEvents(job && !noEventFile ? id : null);
  const run = settledRun(events.run, job?.status, events.phase);
  const now = useNow(running || run.lanes.some(laneActive));
  const settled = events.phase === 'finished' || events.phase === 'error';
  const empty = job !== null && (noEventFile || (settled && run.lastSeq === 0));

  const cancel = async () => {
    setCancelling(true);
    setCancelError('');
    try {
      await cancelDebate(id);
      detail.reload();
      onChanged();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setCancelError(tr.agents.detail.cancelFailed(errorText(err)));
    } finally {
      setCancelling(false);
    }
  };

  if (detail.error !== null && !job) {
    return <Notice tone="crit">{tr.agents.detail.loadFailed(id, detail.error)}</Notice>;
  }

  const wikiBuild = isWikiJob(job) || run.kind === 'code_wiki';
  const task = job?.task || run.task;
  const team = run.team || job?.team || tr.agents.detail.defaultTeam;
  const runMs = runElapsedMs(run, now);
  const elapsed = job ? elapsedText(job, now) : runMs !== null ? fmtDuration(runMs) : '-';
  const readCost = retrievalText(runRetrieval(run) ?? job?.retrieval ?? null);
  const live = running && (events.phase === 'live' || events.phase === 'reconnecting');
  // A finished debate is toned by how it ended (approved, not approved, disputed, ...), not by the job having exited.
  const decision = status === 'completed' && !wikiBuild ? runDecision(run) : null;
  const tone = decision?.tone ?? jobStatusTone(status);

  return (
    <Stack gap="var(--sg-s-6)">
      <Card tone={tone}>
        <Row style={{ justifyContent: 'space-between' }}>
          <Row>
            <StatusChip tone={tone} title={status}>
              {tr.agents.jobStatus[status] ?? status}
            </StatusChip>
            {wikiBuild ? <StatusChip tone="info">{WIKI_JOB_LABEL}</StatusChip> : null}
            {decision ? (
              <StatusChip tone={decision.tone} title={decision.label}>
                {decisionText(decision.label)}
              </StatusChip>
            ) : null}
            <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>
              {tr.agents.detail.elapsed}
              <span data-testid="run-elapsed">{elapsed}</span>
            </span>
            {run.round ? (
              <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{tr.agents.detail.round(run.round, run.maxRounds)}</span>
            ) : null}
            {live ? (
              events.phase === 'live' ? (
                <Row gap="var(--sg-s-2)">
                  <StatusDot tone="info" pulse label={tr.agents.detail.live} />
                  <span style={{ ...muted, fontSize: 12 }}>{tr.agents.detail.live}</span>
                </Row>
              ) : (
                <StatusChip tone="warn">{tr.agents.detail.reconnecting}</StatusChip>
              )
            ) : null}
          </Row>
          {running ? (
            <CancelDebateButton cancelling={cancelling} wiki={wikiBuild} onConfirm={() => void cancel()} />
          ) : null}
        </Row>
        <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }}>{task}</div>
        <Row gap="var(--sg-s-6)" style={{ fontSize: 12, color: 'var(--sg-text-secondary)' }}>
          <span>{tr.agents.detail.project(run.project || job?.project || 'default')}</span>
          <span>{tr.agents.detail.team(team)}</span>
          {run.host ? <span title={tr.agents.detail.hostHint} data-testid="run-host">{tr.agents.detail.host(run.host)}</span> : null}
          {wikiBuild ? <a href={hrefFor('debates', ['wiki'])}>{tr.agents.detail.openWiki}</a> : null}
          {readCost ? <span data-testid="run-retrieval">{readCost}</span> : null}
          {run.sha ? (
            <span>
              {tr.agents.detail.sha}
              <span style={mono} title={run.sha}>
                {run.sha.slice(0, 7)}
              </span>
            </span>
          ) : null}
          {job?.thread_id ? <span style={{ ...mono, overflowWrap: 'anywhere' }}>{job.thread_id}</span> : null}
          {job?.thread_id && !running ? <ReportButton thread={job.thread_id} /> : null}
          {job?.thread_id && !running ? <ArtifactsLink thread={job.thread_id} project={run.project || job.project} /> : null}
        </Row>
        {job?.error ? <Notice tone="crit">{job.error}</Notice> : null}
        {cancelError ? <Notice tone="crit">{cancelError}</Notice> : null}
        {run.errors.map((m, i) => (
          <Notice key={i} tone="crit">
            {m}
          </Notice>
        ))}
      </Card>

      {events.error ? (
        <Notice tone="crit">
          {tr.agents.detail.eventStreamFailed(events.error)}
          <div style={{ marginTop: 'var(--sg-s-3)' }}>
            <Button size="sm" variant="outline" onClick={events.reload}>
              {tr.common.retry}
            </Button>
          </div>
        </Notice>
      ) : null}

      {empty ? (
        <Stack gap="var(--sg-s-3)">
          <Card>
            <strong>{tr.agents.detail.noRecordTitle}</strong>
            <span style={{ ...muted, fontSize: 13 }}>{tr.agents.detail.noRecordBody}</span>
          </Card>
          <Caption>{tr.agents.detail.progressLog}</Caption>
          <div role="log" aria-label={tr.agents.detail.progressLog} tabIndex={0} style={{ maxHeight: 360, overflowY: 'auto', borderRadius: 'var(--sg-r-md)' }}>
            <CodeBlock code={detail.data?.log || (running ? tr.agents.detail.waitingOutput : tr.agents.detail.noLog)} copyable={false} />
          </div>
        </Stack>
      ) : (
        <>
          {run.lanes.length === 0 && events.phase === 'loading' ? <div style={{ ...muted, fontSize: 13 }}>{tr.agents.detail.loadingEvents}</div> : null}
          <LaneCards lanes={run.lanes} now={now} />
          {run.truncated ? <Notice tone="warn">{tr.agents.detail.truncated}</Notice> : null}
          {wikiBuild ? <WikiOutcome run={run} /> : <Outcome run={run} />}
          {run.timeline.length ? (
            <Stack gap="var(--sg-s-3)">
              <Caption>{tr.agents.detail.timeline}</Caption>
              <Timeline run={run} lanes={run.lanes} jobId={id} />
            </Stack>
          ) : running && events.phase !== 'loading' ? (
            <div style={{ ...muted, fontSize: 13 }}>{tr.agents.detail.waitingFirst}</div>
          ) : null}
        </>
      )}
    </Stack>
  );
}

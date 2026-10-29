import { useLayoutEffect, useRef } from 'react';
import { Button, CodeBlock, StatusChip } from '@server-guardian/ui';
import { CancelDebateButton } from '../../components/CancelDebateButton';
import { Caption, Card } from '../../components/Card';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono } from '../../components/layout';
import { useNow } from '../../hooks/useNow';
import { elapsedText, jobStatusTone } from '../../lib/debate';
import type { DebateJobDetail } from '../../types';
import { ArtifactsLink } from './ArtifactsLink';
import { ReportButton } from './ReportButton';
import { ResultView } from './ResultView';
import type { RerunInputs } from './StartForm';
import { tr } from '../../i18n';

/** Scrolling progress log that follows new output unless the user scrolled up. */
function LogView({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <div
      ref={ref}
      role="log"
      aria-label={tr.debate.job.progressLog}
      tabIndex={0}
      onScroll={() => {
        const el = ref.current;
        if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
      style={{ maxHeight: 260, overflowY: 'auto', borderRadius: 'var(--sg-r-md)' }}
    >
      <CodeBlock code={text} copyable={false} />
    </div>
  );
}

/** A finished (or just-settled) debate: status, saved log and the result with verdict, approvals and Publish. */
export function JobResult({
  job,
  error,
  cancelling,
  onCancel,
  onContinue,
  onRerun,
  onOpenAction,
  onActionsChanged,
  onThreadChanged,
}: {
  job: DebateJobDetail;
  error: string | null;
  cancelling: boolean;
  onCancel: () => void;
  onContinue: (thread: string) => void;
  onRerun: (inputs: RerunInputs) => void;
  onOpenAction: (id: number) => void;
  onActionsChanged: () => void;
  onThreadChanged: () => void;
}) {
  const running = job.status === 'running';
  const now = useNow(running);
  return (
    <Stack gap="var(--sg-s-6)">
      <Card tone={jobStatusTone(job.status)}>
        <Row style={{ justifyContent: 'space-between' }}>
          <Row>
            <StatusChip tone={jobStatusTone(job.status)} title={job.status}>
              {tr.debate.jobStatus[job.status] ?? job.status.toUpperCase()}
            </StatusChip>
            <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>
              {`${tr.debate.job.elapsed} `}
              <span data-testid="job-elapsed">{elapsedText(job, now)}</span>
            </span>
            <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{tr.debate.job.team(job.team || tr.debate.lib.defaultTeam)}</span>
            {job.thread_id ? <span style={{ ...mono, color: 'var(--sg-text-secondary)', fontSize: 13, overflowWrap: 'anywhere' }}>{job.thread_id}</span> : null}
            {job.thread_id && !running ? <ReportButton thread={job.thread_id} /> : null}
            {job.thread_id && !running ? <ArtifactsLink thread={job.thread_id} project={job.project} /> : null}
            {job.thread_closed ? <StatusChip tone="idle">{tr.debate.job.closed}</StatusChip> : null}
            {job.thread_published ? <StatusChip tone="ok">{tr.debate.job.published}</StatusChip> : null}
          </Row>
          {running ? <CancelDebateButton cancelling={cancelling} onConfirm={onCancel} /> : (
            // Whatever way the debate ended (done, failed, cancelled, thread closed), its inputs can be run again.
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                onRerun({
                  project: job.project,
                  task: job.task,
                  team: job.team,
                  rounds: job.rounds,
                  contextPaths: job.context_paths ?? [],
                  contextRef: job.context_ref ?? '',
                })
              }
            >
              {tr.debate.job.runAgain}
            </Button>
          )}
        </Row>
        <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 13 }}>{job.task}</div>
        {job.error ? <Notice tone="crit">{job.error}</Notice> : null}
        {error ? <Notice tone="crit">{error}</Notice> : null}
      </Card>
      <Stack gap="var(--sg-s-2)">
        <Caption>{running ? tr.debate.job.progressLogLive : tr.debate.job.progressLog}</Caption>
        <LogView key={job.id} text={job.log || (running ? tr.debate.job.waitingOutput : tr.debate.job.noLog)} />
      </Stack>
      {job.result ? (
        <ResultView
          result={job.result}
          job={job}
          onContinue={onContinue}
          onOpenAction={onOpenAction}
          onActionsChanged={onActionsChanged}
          onThreadChanged={onThreadChanged}
        />
      ) : job.status === 'completed' ? (
        <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{tr.debate.job.noResult}</div>
      ) : null}
    </Stack>
  );
}

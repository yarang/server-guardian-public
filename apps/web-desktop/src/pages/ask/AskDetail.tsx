import { useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { fetchSolo } from '../../api';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { jobStatusTone } from '../../lib/debate';
import { TASK_MAX } from '../../lib/debate';
import { SOLO_POLL_MS, askParams, debateTaskFrom, isGeneral, stripFindings } from '../../lib/solo';
import { hrefFor, navigate } from '../../router';
import { setDebatePrefill } from '../../state/debatePrefill';
import type { SoloView } from '../../types';
import { tr } from '../../i18n';

/**
 * Follows one solo run: polled every {@link SOLO_POLL_MS} while it runs, then fetched once more (a finished
 * run never changes; the global Refresh refetches it). The status of the last answer decides whether to poll.
 */
function useSoloRun(id: string) {
  const [status, setStatus] = useState('');
  return useHubData(
    () =>
      fetchSolo(id).then((view) => {
        setStatus(view.status);
        return view;
      }),
    { pollMs: status === 'running' ? SOLO_POLL_MS : undefined },
  );
}

/** One solo run: the SOLO label with its note, the facts of the answer, and the answer itself. */
function SoloRunView({ view }: { view: SoloView }) {
  const result = view.result;
  // A general question has no project and no sources; its own note says so, and a debate cannot take it.
  const general = isGeneral(result) || isGeneral(view);
  // The findings tail is machine output (the audit convention), not prose: hide it from reading and copying.
  const answer = result ? stripFindings(result.answer) : '';
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(answer).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }, () => undefined);
  };
  const runAsDebate = () => {
    if (!result) return;
    setDebatePrefill(debateTaskFrom(result.question, answer, tr.ask.earlier, TASK_MAX), 'ask');
    navigate('debates');
  };
  return (
    <Stack gap="var(--sg-s-5)">
      <div>
        <a href={hrefFor('debates', askParams({}))}>{tr.ask.back}</a>
      </div>
      <Card>
        <Row>
          <StatusChip tone="idle">{view.label}</StatusChip>
          <span style={{ ...muted, fontSize: 12 }}>{general ? result?.note || view.note || tr.ask.note : tr.ask.note}</span>
          <StatusChip tone={jobStatusTone(view.status)} title={view.status}>
            {tr.agents.jobStatus[view.status] ?? view.status}
          </StatusChip>
        </Row>
        {result ? (
          <>
            <strong style={{ overflowWrap: 'anywhere' }}>{result.question}</strong>
            <Row>
              <span style={{ ...mono, ...muted, fontSize: 12 }}>
                {general ? tr.ask.generalLabel : tr.ask.project(result.project)}
              </span>
              <span style={{ ...mono, ...muted, fontSize: 12 }}>{tr.ask.roleLabel(result.role)}</span>
              <span style={{ ...mono, ...muted, fontSize: 12 }}>{tr.ask.providerLabel(result.provider)}</span>
              <span style={{ ...muted, fontSize: 12 }}>{tr.ask.elapsed(result.elapsed_s)}</span>
            </Row>
          </>
        ) : null}
        {view.status === 'running' ? <div style={{ ...muted, fontSize: 13 }}>{tr.ask.waiting}</div> : null}
        {view.status === 'failed' ? <Notice tone="crit">{view.error || tr.ask.failed}</Notice> : null}
        {result?.degraded ? <Notice tone="warn">{tr.ask.degraded}</Notice> : null}
        {result ? (
          <>
            <div style={{ ...muted, fontSize: 12 }}>{general ? tr.ask.noSources : tr.ask.citations(result.summary)}</div>
            <Row>
              <Button size="sm" variant="outline" onClick={copy}>
                {copied ? tr.ask.copied : tr.ask.copy}
              </Button>
              {general ? null : (
                <Button size="sm" variant="outline" onClick={runAsDebate}>
                  {tr.ask.runAsDebate}
                </Button>
              )}
            </Row>
            <MarkdownDoc markdown={answer} topHeading={3} />
          </>
        ) : null}
      </Card>
    </Stack>
  );
}

/** `#/debates/ask/<id>`: one solo run, from its start to its answer (or its failure). */
export function AskDetail({ id }: { id: string }) {
  const state = useSoloRun(id);
  return (
    <DataGate state={state} what={tr.ask.what}>
      {(view) => <SoloRunView view={view} />}
    </DataGate>
  );
}

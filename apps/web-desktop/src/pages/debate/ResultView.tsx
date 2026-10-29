import { Button, Panel, StatusChip, TextField, VerdictChain } from '@server-guardian/ui';
import type { VerdictStep } from '@server-guardian/ui';
import { AuthRequiredError, HubError, closeThread, fetchPublications, getPublishPlan, jevRecheck, listActions } from '../../api';
import { Caption, Card } from '../../components/Card';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Disclosure } from '../../components/Disclosure';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { errorText } from '../../lib/format';
import { useHubAction } from '../../hooks/useHubAction';
import { useOperationsStore } from '../../state/operationsStore';
import { useHubData } from '../../hooks/useHubData';
import { useEffect, useState } from 'react';
import {
  BRANCH_RE,
  actionStatusTone,
  canCloseThread,
  decisionStatus,
  decisionText,
  failMessage,
  fmtKB,
  gateNeedsRecheck,
  groupByRound,
  buildFileChanges,
  hasTraceRefs,
  mentionedFiles,
  unwrittenMentions,
  jevGateTone,
  publishReadiness,
  roleTone,
  teamLabel,
} from '../../lib/debate';
import type { ActionItem, DebateAction, DebateContext, DebateFinalDecision, DebateJob, DebateResult, PublishPlan } from '../../types';
import { RecheckSummary } from './ActionDetails';
import { DecisionView, FileChangesSection, JudgmentSection, RationaleSection, RefList } from './DecisionSections';
import { tr } from '../../i18n';

/** Display label of a jev gate word (PASSED, PENDING, DISPUTED, ...); unknown words stay raw. */
function gateText(gate: string): string {
  const g = gate.toUpperCase();
  return tr.debate.gate[g] ?? g;
}

/** LLM judge -> jev -> gate, built only from fields the result actually carries. */
function verdictSteps(fd: DebateFinalDecision, status: { label: string; tone: VerdictStep['tone'] }): VerdictStep[] {
  const steps: VerdictStep[] = [];
  const judgeApproved = !fd.unverified && (fd.jev_gate !== undefined || fd.approved === true);
  steps.push(
    fd.unverified
      ? { label: tr.debate.result.llmJudge, result: decisionText('UNVERIFIED'), tone: 'warn', detail: fd.llm_error ? tr.debate.result.llmUnavailable(fd.llm_error) : tr.debate.result.cannedFallback }
      : {
          label: tr.debate.result.llmJudge,
          result: judgeApproved ? decisionText('APPROVED') : tr.debate.result.revisionRequired,
          tone: judgeApproved ? 'ok' : 'warn',
          detail: fd.final_round !== undefined ? tr.debate.result.finalRound(fd.final_round) : undefined,
        }
  );
  if (fd.jev_gate !== undefined) {
    const reasons = fd.jev?.reasons?.length ?? 0;
    steps.push({
      label: 'jev-mcp',
      result: gateText(String(fd.jev_gate)),
      tone: jevGateTone(String(fd.jev_gate)),
      detail: reasons ? tr.debate.result.reasonsRecorded(reasons) : undefined,
    });
  }
  const actions = fd.actions?.length ?? 0;
  steps.push({
    label: tr.debate.result.gate,
    result: decisionText(status.label),
    tone: status.tone,
    detail: actions ? tr.debate.result.proposedActions(actions) : undefined,
  });
  return steps;
}

/** "Reference code: abc1234 · 3 files · 12.0 KB" with expandable files and skipped entries. */
export function ContextSummary({ ctx }: { ctx: DebateContext | null | undefined }) {
  if (!ctx) return <div style={{ ...muted, fontSize: 13 }}>{tr.debate.result.noContext}</div>;
  const files = ctx.files || [];
  const skipped = ctx.skipped || [];
  const sha7 = (ctx.sha || '').slice(0, 7);
  return (
    <Card>
      <Disclosure
        summary={
          <>
            {`${tr.debate.result.refCode}: `}
            <span style={mono} title={ctx.sha}>
              {sha7}
            </span>
            {tr.debate.result.contextStats(files.length, fmtKB(ctx.total_bytes), skipped.length)}
          </>
        }
      >
        <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
          {tr.debate.result.contextMeta(ctx.ref, (ctx.paths || []).join(', ') || '-', ctx.tree_files)}
        </div>
        {files.length ? (
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-2)', fontSize: 13 }}>
            {files.map((f) => (
              <li key={f.path} style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-3)', alignItems: 'baseline' }}>
                <span style={{ ...mono, overflowWrap: 'anywhere' }}>{f.path}</span>
                <span style={muted}>{fmtKB(f.bytes)}</span>
                {f.truncated ? <StatusChip tone="warn">{tr.debate.result.truncated}</StatusChip> : null}
                {f.parts && f.parts.length ? (
                  <span style={{ color: 'var(--sg-info)', fontSize: 12, overflowWrap: 'anywhere' }}>{`${f.parts.join(', ')}${f.outline ? tr.debate.result.plusOutline : ''}`}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        {skipped.length ? (
          <>
            <Caption>{tr.debate.result.skipped(skipped.length)}</Caption>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-2)', fontSize: 13 }}>
              {skipped.map((s, i) => (
                <li key={`${s.path}-${i}`} style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-3)', alignItems: 'baseline' }}>
                  <span style={{ ...mono, overflowWrap: 'anywhere' }}>{s.path}</span>
                  <span style={{ color: 'var(--sg-warn)' }}>{s.reason}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </Disclosure>
    </Card>
  );
}

function RecheckPanel({ thread, gate, onChanged }: { thread: string; gate: string; onChanged: () => void }) {
  const recheck = useHubAction(async (t: string) => {
    try {
      const r = await jevRecheck(t);
      onChanged();
      return r;
    } catch (err) {
      if (err instanceof AuthRequiredError) throw err;
      throw new Error(failMessage(err, 'recheck'));
    }
  });
  return (
    <Card>
      <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{tr.debate.result.gateNote(gate)}</div>
      <div>
        <Button variant="outline" loading={recheck.running} onClick={() => void recheck.run(thread)}>
          {tr.debate.result.recheck}
        </Button>
      </div>
      {recheck.error ? <Notice tone="crit">{recheck.error}</Notice> : null}
      {recheck.data ? <RecheckSummary result={recheck.data} /> : null}
    </Card>
  );
}

/** Close-thread button with its confirm step; the thread's records are kept. */
function CloseThreadButton({ thread, onClosed }: { thread: string; onClosed: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const close = useHubAction(async (t: string) => {
    try {
      const r = await closeThread(t);
      setConfirming(false);
      onClosed();
      return r;
    } catch (err) {
      if (err instanceof AuthRequiredError) throw err;
      throw new Error(failMessage(err, 'close'));
    }
  });
  return (
    <>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          close.reset();
          setConfirming(true);
        }}
      >
        {tr.debate.result.closeThread}
      </Button>
      {confirming ? (
        <ConfirmModal
          title={tr.debate.result.closeThread}
          message={tr.debate.result.closeConfirm(thread)}
          confirmLabel={tr.debate.result.closeThread}
          tone="primary"
          busy={close.running}
          error={close.error}
          onConfirm={() => void close.run(thread)}
          onCancel={() => setConfirming(false)}
        />
      ) : null}
    </>
  );
}

type PlanState = { state: 'idle' | 'loading' } | { state: 'ready'; plan: PublishPlan } | { state: 'error'; error: string };

/** The plan the hub would follow, as a short summary list. */
function PlanSummary({ plan }: { plan: PublishPlan }) {
  return (
    <Stack gap="var(--sg-s-2)">
      <div style={{ fontSize: 13, display: 'grid', gridTemplateColumns: 'max-content 1fr', columnGap: 'var(--sg-s-5)', rowGap: 'var(--sg-s-2)' }}>
        <span style={muted}>{tr.debate.result.branch}</span>
        <span style={mono}>{plan.branch}</span>
        <span style={muted}>{tr.debate.result.basedOn}</span>
        <span style={mono} title={plan.base_sha}>
          {(plan.base_sha || '').slice(0, 7)}
        </span>
        <span style={muted}>{tr.debate.result.remote}</span>
        <span style={mono}>{plan.remote}</span>
        <span style={muted}>{tr.debate.result.commit}</span>
        <span>{plan.subject}</span>
        <span style={muted}>{tr.debate.result.files(plan.files.length)}</span>
        <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-7)', ...mono }}>
          {plan.files.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      </div>
      {plan.retry ? <Notice tone="info">{tr.debate.result.branchExists}</Notice> : null}
    </Stack>
  );
}

/**
 * Publish: commits the thread's executed file actions to a new branch of the hub workspace and pushes it.
 * The hub refuses unless every action is decided, the workspace still matches and the project's test passes;
 * the confirm step shows the hub's plan first and stays disabled while the plan is refused.
 */
function PublishPanel({ thread, ready, reason }: { thread: string; ready: boolean; reason: string }) {
  const [confirming, setConfirming] = useState(false);
  const [branch, setBranch] = useState('');
  const [plan, setPlan] = useState<PlanState>({ state: 'idle' });
  const branchValue = branch.trim();
  const branchOk = branchValue === '' || BRANCH_RE.test(branchValue);

  useEffect(() => {
    if (!confirming) return;
    if (!branchOk) {
      setPlan({ state: 'idle' });
      return;
    }
    let cancelled = false;
    setPlan({ state: 'loading' });
    // A typed branch is looked up after a short pause; the default branch immediately.
    const timer = window.setTimeout(
      () => {
        getPublishPlan(thread, branchValue || undefined).then(
          (p) => {
            if (!cancelled) setPlan({ state: 'ready', plan: p });
          },
          (err: unknown) => {
            if (err instanceof AuthRequiredError) window.dispatchEvent(new CustomEvent('guardian:auth-required'));
            if (!cancelled) setPlan({ state: 'error', error: errorText(err) });
          }
        );
      },
      branchValue ? 300 : 0
    );
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [confirming, thread, branchValue, branchOk]);

  // The publish runs in the operations store, so leaving the page keeps its outcome for when the operator is back.
  const ops = useOperationsStore();
  const entry = ops.publishes[thread];
  const running = entry?.phase === 'running';
  const r = entry?.phase === 'done' ? entry.response : null;
  const publishError = entry?.phase === 'error' ? entry.message : null;
  const publications = useHubData(() => fetchPublications(thread));
  const reloadPublications = publications.reload;
  const settledPhase = entry && entry.phase !== 'running' ? entry.phase : null;
  useEffect(() => {
    if (settledPhase === 'done') setConfirming(false);
    if (settledPhase) reloadPublications();
  }, [settledPhase, reloadPublications]);
  const pushed = (publications.data ?? []).find((p) => p.status.toUpperCase() === 'PUSHED') ?? null;
  const canPublish = ready && !pushed;
  return (
    <>
      <Row>
        <Button
          size="sm"
          variant="outline"
          disabled={!canPublish || running}
          loading={running}
          onClick={() => {
            ops.clearPublish(thread);
            setPlan({ state: 'idle' });
            setConfirming(true);
          }}
        >
          {tr.debate.result.publish}
        </Button>
        <span style={{ ...muted, fontSize: 12 }}>
          {pushed
            ? tr.debate.result.alreadyPublished(pushed.branch, pushed.commit_sha ? pushed.commit_sha.slice(0, 7) : '')
            : running
              ? tr.debate.result.publishingNote
              : ready
                ? tr.debate.result.publishNote
                : `${reason} ${tr.debate.result.publishNeeds}`}
        </span>
      </Row>
      {pushed ? (
        <Notice tone="ok">{tr.debate.result.publishedAs(pushed.branch, pushed.commit_sha ? pushed.commit_sha.slice(0, 7) : '')}</Notice>
      ) : null}
      {publishError && !confirming ? <Notice tone="crit">{publishError}</Notice> : null}
      {r && r.ok && !pushed ? (
        <Notice tone="ok">{tr.debate.result.pushed(r.branch ?? '', (r.commit_sha ?? '').slice(0, 12), r.files?.length ? r.files.join(', ') : '')}</Notice>
      ) : null}
      {r && !r.ok ? (
        <Notice tone="crit">{`${tr.debate.result.pushFailed(r.branch ?? '')}\n${r.stderr || r.error || ''}`}</Notice>
      ) : null}
      {confirming ? (
        <ConfirmModal
          title={tr.debate.result.publishThread}
          tone="primary"
          message={
            <Stack gap="var(--sg-s-4)">
              <span>{tr.debate.result.publishConfirm(thread)}</span>
              <TextField
                label={tr.debate.result.branchOptional}
                mono
                placeholder={`guardian/${thread}`}
                value={branch}
                error={branchOk ? undefined : tr.debate.result.branchInvalid}
                onChange={(e) => setBranch(e.target.value)}
              />
              {plan.state === 'loading' ? <span style={{ ...muted, fontSize: 13 }}>{tr.debate.result.checkingPlan}</span> : null}
              {plan.state === 'ready' ? <PlanSummary plan={plan.plan} /> : null}
              {plan.state === 'error' ? <Notice tone="crit">{plan.error}</Notice> : null}
            </Stack>
          }
          confirmLabel={tr.debate.result.publish}
          confirmDisabled={!branchOk || plan.state !== 'ready'}
          busy={running}
          error={publishError}
          onConfirm={() => ops.startPublish(thread, branch)}
          onCancel={() => setConfirming(false)}
        />
      ) : null}
    </>
  );
}

/** Everything a finished debate produced; a proposed action opens on the Approvals page. */
type TranscriptItem = NonNullable<DebateResult['transcript']>[number];

/**
 * The transcript by round, one foldable card per agent answer. Answers start folded (the judgment is shown
 * in full above); the Markdown of a card is rendered only while it is open.
 */
function Transcript({ rounds }: { rounds: Array<[number, TranscriptItem[]]> }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const keysOf = (round: number, items: TranscriptItem[]) => items.map((_, i) => `${round}:${i}`);
  const all = rounds.flatMap(([round, items]) => keysOf(round, items));
  const setMany = (keys: string[], on: boolean) =>
    setOpen((prev) => {
      const next = new Set(prev);
      keys.forEach((k) => (on ? next.add(k) : next.delete(k)));
      return next;
    });
  if (rounds.length === 0) return null;
  return (
    <Stack gap="var(--sg-s-4)">
      <Row style={{ justifyContent: 'flex-end' }} gap="var(--sg-s-3)">
        <Button size="sm" variant="outline" onClick={() => setMany(all, true)} disabled={open.size === all.length}>
          {tr.debate.result.expandAll}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setMany(all, false)} disabled={open.size === 0}>
          {tr.debate.result.collapseAll}
        </Button>
      </Row>
      {rounds.map(([round, items]) => {
        const keys = keysOf(round, items);
        const roundOpen = keys.every((k) => open.has(k));
        return (
          <Stack key={round} gap="var(--sg-s-3)">
            <Row style={{ justifyContent: 'space-between' }}>
              <h4 style={{ margin: 0, fontSize: 14, color: 'var(--sg-info)' }}>{tr.debate.result.round(round)}</h4>
              <button
                type="button"
                onClick={() => setMany(keys, !roundOpen)}
                style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', color: 'var(--sg-text-secondary)', fontSize: 12 }}
              >
                {roundOpen ? tr.debate.result.collapseRound(round) : tr.debate.result.expandRound(round)}
              </button>
            </Row>
            {items.map((item, i) => {
              const key = keys[i];
              const isOpen = open.has(key);
              return (
                <Card key={key}>
                  <details open={isOpen} onToggle={(e) => setMany([key], e.currentTarget.open)} style={{ minWidth: 0 }}>
                    <summary style={{ cursor: 'pointer', listStyle: 'revert' }} aria-label={tr.debate.result.answerToggle(item.agent, round)}>
                      <span style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)', verticalAlign: 'middle' }}>
                        <strong style={{ fontSize: 13 }}>{item.agent}</strong>
                        <StatusChip tone={roleTone(item.role || '')}>{item.role}</StatusChip>
                        <span style={{ ...mono, ...muted, fontSize: 12 }}>{item.provider || tr.debate.result.unknownProvider}</span>
                        <span style={{ ...muted, fontSize: 12 }}>{fmtKB(new TextEncoder().encode(item.content || '').length)}</span>
                      </span>
                    </summary>
                    {(item.fallbacks || []).map((f, j) => (
                      <div key={j} style={{ color: 'var(--sg-warn)', fontSize: 12, overflowWrap: 'anywhere', marginTop: 'var(--sg-s-2)' }}>
                        {tr.debate.result.skippedProvider(f)}
                      </div>
                    ))}
                    {isOpen ? (
                      <div style={{ marginTop: 'var(--sg-s-3)' }}>
                        <MarkdownDoc markdown={item.content || ''} topHeading={4} />
                      </div>
                    ) : null}
                  </details>
                </Card>
              );
            })}
          </Stack>
        );
      })}
    </Stack>
  );
}

export function ResultView({
  result,
  job,
  onContinue,
  onOpenAction,
  onActionsChanged,
  onThreadChanged,
}: {
  result: DebateResult;
  job: DebateJob;
  onContinue: (thread: string) => void;
  onOpenAction: (id: number) => void;
  onActionsChanged: () => void;
  /** Called after the thread was closed, so the shown job and the lists are reloaded. */
  onThreadChanged?: () => void;
}) {
  const fd = result.final_decision || null;
  const status = decisionStatus(fd);
  const threadId = result.thread_id || job.thread_id || '';
  const rounds = groupByRound(result.transcript || []);
  const reasons = fd?.jev?.reasons || [];
  const snapshot = fd?.actions || [];
  // result.json holds the statuses from proposal time; the live list reflects Execute / Reject since then.
  const live = useHubData<ActionItem[]>(() => (threadId ? listActions(threadId) : Promise.resolve([])));
  // An empty live list while the result names actions is not trusted; the snapshot is shown instead.
  const liveActions = live.data && live.data.length > 0 ? live.data : null;
  const actions: DebateAction[] = liveActions
    ? liveActions.map((a) => {
        const result = a.result as { error?: unknown } | null | undefined;
        return {
          id: a.id,
          tool: a.tool,
          status: String(a.status),
          error: typeof result?.error === 'string' ? result.error : snapshot.find((x) => String(x.id) === String(a.id))?.error,
        };
      })
    : snapshot;
  const readiness = liveActions
    ? publishReadiness(liveActions)
    : { ready: false, reason: live.loading ? tr.debate.result.loadingStatuses : tr.debate.result.statusesUnavailable };

  const fileChanges = buildFileChanges(liveActions, snapshot);
  const traced = fd ? hasTraceRefs(fd) : false;
  const unwritten = fd
    ? unwrittenMentions(
        mentionedFiles([fd.decision ?? '', ...(fd.constraints ?? []), ...(fd.checks ?? [])]),
        fileChanges.map((c) => c.path)
      )
    : [];

  const actionColumns: Column<DebateAction>[] = [
    {
      header: 'ID',
      mono: true,
      render: (a) => (
        <Button size="sm" variant="outline" aria-label={tr.debate.result.openAction(a.id)} onClick={() => onOpenAction(Number(a.id))}>
          {`#${a.id}`}
        </Button>
      ),
    },
    { header: tr.debate.result.colTool, mono: true, render: (a) => a.tool },
    { header: tr.debate.result.colStatus, render: (a) => (
        <StatusChip tone={actionStatusTone(String(a.status))} title={String(a.status)}>
          {tr.debate.actionStatus[String(a.status).toUpperCase()] ?? String(a.status)}
        </StatusChip>
      ) },
    { header: tr.debate.result.colError, render: (a) => (a.error ? <span style={{ color: 'var(--sg-crit)' }}>{a.error}</span> : '') },
  ];

  return (
    <Stack gap="var(--sg-s-6)">
      <Panel
        title={tr.debate.result.title}
        tone={status.tone}
        actions={
          threadId ? (
            <Row>
              {canCloseThread(status.label, job.thread_closed) ? (
                <CloseThreadButton thread={threadId} onClosed={() => onThreadChanged?.()} />
              ) : null}
              {job.thread_published ? (
                <span style={{ ...muted, fontSize: 12 }}>{tr.debate.result.publishedFollowUp}</span>
              ) : (
                <Button size="sm" variant="outline" onClick={() => onContinue(threadId)}>
                  {job.thread_closed ? tr.debate.result.reopen : tr.debate.result.continueThread}
                </Button>
              )}
            </Row>
          ) : undefined
        }
      >
        <Stack gap="var(--sg-s-5)">
          <Row>
            <StatusChip tone={status.tone} title={status.label}>
              {decisionText(status.label)}
            </StatusChip>
            {job.thread_closed ? <StatusChip tone="idle">{tr.debate.job.closed}</StatusChip> : null}
            {job.thread_published ? <StatusChip tone="ok">{tr.debate.job.published}</StatusChip> : null}
            <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{tr.debate.job.team(teamLabel(result.team))}</span>
            {threadId ? <span style={{ ...mono, color: 'var(--sg-text-secondary)', fontSize: 13, overflowWrap: 'anywhere' }}>{tr.debate.result.thread(threadId)}</span> : null}
          </Row>
          {fd ? <VerdictChain steps={verdictSteps(fd, status)} /> : null}
          {fd?.decision ? <DecisionView fd={fd} files={fileChanges} traced={traced} /> : null}
          {fd?.rationale ? <RationaleSection text={fd.rationale} /> : null}
          {fd?.constraints && fd.constraints.length ? <RefList caption={tr.debate.result.constraints} items={fd.constraints} refs={fd.constraint_refs} /> : null}
          {fd?.checks && fd.checks.length ? <RefList caption={tr.debate.result.checks} items={fd.checks} refs={fd.check_refs} /> : null}
          <FileChangesSection changes={fileChanges} unwritten={unwritten} />
          <JudgmentSection judgment={fd?.judgment} />
          {result.log_file ? <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{tr.debate.result.logFile(result.log_file)}</div> : null}
        </Stack>
      </Panel>

      <ContextSummary ctx={result.context} />

      {reasons.length ? (
        <Card tone="warn">
          <Caption>{tr.debate.result.gateReasons(fd?.jev_gate ? String(fd.jev_gate) : '')}</Caption>
          <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-9)', lineHeight: 1.6, fontSize: 13 }}>
            {reasons.map((r, i) => (
              <li key={i} style={{ overflowWrap: 'anywhere' }}>
                {r}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {threadId && gateNeedsRecheck(fd) ? (
        <RecheckPanel
          thread={threadId}
          gate={String(fd?.jev_gate)}
          onChanged={() => {
            live.reload();
            onActionsChanged();
          }}
        />
      ) : null}

      {actions.length ? (
        <Card>
          <Caption>{tr.debate.result.approvals(actions.length)}</Caption>
          <DataTable label={tr.debate.result.actionsLabel} columns={actionColumns} rows={actions} getKey={(a) => String(a.id)} />
          <div style={{ ...muted, fontSize: 12 }}>{tr.debate.result.openActionNote}</div>
          {threadId ? <PublishPanel thread={threadId} ready={readiness.ready} reason={readiness.reason} /> : null}
        </Card>
      ) : null}

      <Transcript rounds={rounds} />
    </Stack>
  );
}

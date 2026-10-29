import { useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { fmtDateTime } from '../../lib/format';
import { Disclosure } from '../../components/Disclosure';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Row, mono, muted } from '../../components/layout';
import { actionStatusTone, decisionStatus, decisionText, decisionWordStatus, fmtDuration, fmtKB, jevGateTone, roleTone } from '../../lib/debate';
import { actionLabel, gateLabel, offsetLabel, requestLabel } from '../../lib/agentMonitor';
import { wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import type { Lane, RunState, TimelineItem } from '../../lib/agentMonitor';
import { hrefFor } from '../../router';
import { fetchDebatePrompt } from '../../api';
import type { RetrievalRequest, RetrievalResult } from '../../types';

const chipBox: CSSProperties = {
  ...mono,
  display: 'inline-block',
  maxWidth: '100%',
  padding: '2px var(--sg-s-3)',
  background: 'var(--sg-code-bg)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  fontSize: 12,
  overflowWrap: 'anywhere',
};

/** One retrieval request as a chip, with the served result next to it once the engine answered. */
function RequestRow({ request, result, served }: { request: RetrievalRequest; result?: RetrievalResult; served: boolean }) {
  let outcome: ReactNode;
  if (!served) {
    outcome = <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.waiting}</span>;
  } else if (!result) {
    outcome = <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.noResult}</span>;
  } else if (result.deferred || result.error === 'deferred') {
    outcome = (
      <span title={tr.agents.timeline.deferredHint}>
        <StatusChip tone="warn">{tr.agents.timeline.deferred}</StatusChip>
      </span>
    );
  } else if (result.error === 'budget') {
    outcome = <StatusChip tone="warn">{tr.agents.timeline.budget}</StatusChip>;
  } else if (result.error === 'excluded') {
    outcome = <StatusChip tone="warn">{tr.agents.timeline.excluded}</StatusChip>;
  } else if (!result.ok || result.error) {
    outcome = (
      <span title={result.error}>
        <StatusChip tone="crit">{result.error ? tr.agents.timeline.errorWith(result.error.slice(0, 60)) : tr.agents.timeline.error}</StatusChip>
      </span>
    );
  } else {
    outcome = (
      <>
        <span style={{ ...muted, fontSize: 12 }}>{fmtKB(result.bytes)}</span>
        {result.truncated ? <StatusChip tone="warn">{tr.agents.timeline.truncatedChip}</StatusChip> : null}
      </>
    );
  }
  // Legacy runs: one aggregate `?` item stood for every request over the limit.
  if (result && result.op === '?') {
    return <Notice tone="warn">{result.error || `${request.target || tr.agents.timeline.repoBlock}: ${tr.agents.timeline.tooManyRequests}`}</Notice>;
  }
  return (
    <Row gap="var(--sg-s-3)">
      <span style={chipBox}>{requestLabel(request)}</span>
      {outcome}
    </Row>
  );
}

/** The turn prompt of one agent_started event, fetched from the hub the first time it is opened. */
function PromptView({ jobId, seq, truncated }: { jobId: string; seq: number; truncated: boolean }) {
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'ok' | 'error'; text: string }>({ status: 'idle', text: '' });
  const [copied, setCopied] = useState(false);
  const load = (open: boolean) => {
    if (!open || state.status === 'loading' || state.status === 'ok') return;
    setState({ status: 'loading', text: '' });
    fetchDebatePrompt(jobId, seq).then(
      (p) => setState({ status: 'ok', text: p.text }),
      (err: unknown) => setState({ status: 'error', text: err instanceof Error ? err.message : String(err) })
    );
  };
  const copy = () => {
    navigator.clipboard?.writeText(state.text).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }, () => undefined);
  };
  return (
    <Disclosure summary={tr.agents.timeline.showPrompt} onToggle={load}>
      {state.status === 'loading' || state.status === 'idle' ? <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.promptLoading}</span> : null}
      {state.status === 'error' ? <Notice tone="crit">{tr.agents.timeline.promptError(state.text)}</Notice> : null}
      {state.status === 'ok' ? (
        <>
          {truncated ? <Notice tone="warn">{tr.agents.timeline.promptTruncated}</Notice> : null}
          <Row>
            <Button size="sm" variant="outline" onClick={copy}>
              {copied ? tr.agents.timeline.copied : tr.agents.timeline.copyPrompt}
            </Button>
          </Row>
          <MonoBlock text={state.text} maxHeight={480} />
        </>
      ) : null}
    </Disclosure>
  );
}

function Who({ name, role }: { name: string; role: string }) {
  return <StatusChip tone={roleTone(role)}>{name || 'agent'}</StatusChip>;
}

function itemBody(item: TimelineItem, role: (agent: string) => string, jobId: string | null): ReactNode {
  switch (item.kind) {
    case 'run_started':
      return (
        <Row>
          <strong>{tr.agents.timeline.debateStarted}</strong>
          <span style={{ ...muted, fontSize: 12 }}>
            {tr.agents.timeline.runStartedInfo(item.members.length, item.maxRounds, item.files)}
          </span>
          {item.sha ? <span style={{ ...mono, ...muted, fontSize: 12 }} title={item.sha}>{item.sha.slice(0, 7)}</span> : null}
        </Row>
      );
    case 'agent_started':
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <Who name={item.name} role={item.role || role(item.agent)} />
            <span>{tr.agents.timeline.started}</span>
            {item.resumed ? <StatusChip tone="idle">{tr.agents.timeline.sessionResumed}</StatusChip> : null}
            {item.promptBytes !== null ? <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.prompt(fmtKB(item.promptBytes))}</span> : null}
          </Row>
          {item.promptSaved && jobId ? <PromptView jobId={jobId} seq={item.seq} truncated={item.promptTruncated} /> : null}
        </div>
      );
    case 'retrieval': {
      const rows = item.requests.length ? item.requests : (item.results ?? []).map((r) => ({ op: r.op, target: r.target }));
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <Who name={item.name} role={role(item.agent)} />
            <span>{item.via === 'ops' ? tr.agents.timeline.checksNodes(item.step) : tr.agents.timeline.readsCode(item.step)}</span>
            {item.via === 'mcp' ? <StatusChip tone="info">MCP</StatusChip> : null}
            {item.turnBytes !== null ? <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.thisTurn(fmtKB(item.turnBytes))}</span> : null}
          </Row>
          {rows.map((r, i) => (
            <RequestRow key={i} request={r} result={item.results?.[i]} served={item.results !== null} />
          ))}
        </div>
      );
    }
    case 'reply_trimmed':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.replyTrimmed(item.name, item.discardedChars, item.blocks)}
        </Notice>
      );
    case 'action_repair': {
      const n = item.invalid.length;
      const lines = item.invalid.map((x) => `${x.index !== null ? `#${x.index} ` : ''}${x.tool || tr.agents.detail.actionFallback}: ${x.error || tr.agents.timeline.invalid}`);
      const long = n > 2 || lines.some((l) => l.length > 120);
      const list = <div style={{ ...mono, fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{lines.join('\n')}</div>;
      return (
        <Notice tone="warn">
          <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
            <span>{tr.agents.timeline.actionRepair(item.name || tr.agents.timeline.judge, n, item.fixed)}</span>
            {n ? long ? <Disclosure summary={tr.agents.timeline.showErrors}>{list}</Disclosure> : list : null}
          </div>
        </Notice>
      );
    }
    case 'format_retry':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.formatRetry(item.name || tr.agents.timeline.judge, item.missing.length ? item.missing.join('/') : tr.agents.timeline.omittedDefault)}
        </Notice>
      );
    case 'pseudo_tool_call':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.pseudoCall(item.name, item.tools.join(', '), item.discardedChars)}
        </Notice>
      );
    case 'citation_check':
      if (item.unverified.length === 0) {
        return (
          <Row>
            <Who name={item.name} role={role(item.agent)} />
            <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.citationsVerified(item.cited)}</span>
          </Row>
        );
      }
      return (
        <Notice tone="warn">
          {tr.agents.timeline.citationsUnverified(item.name, item.unverified.length, item.cited, item.asked, item.unverified.join('\n'))}
        </Notice>
      );
    case 'exhausted':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.exhausted(item.name, item.steps, fmtKB(item.turnBytes))}
        </Notice>
      );
    case 'provider_failed':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.providerFailed(item.name, item.provider || tr.common.unknown, item.error)}
        </Notice>
      );
    case 'agent_finished':
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <Who name={item.name} role={role(item.agent)} />
            <span>{tr.agents.timeline.finished}</span>
            {item.builtin ? <StatusChip tone="warn">{tr.agents.timeline.builtinFallback}</StatusChip> : item.provider ? <span style={{ ...mono, fontSize: 12 }}>{item.provider}</span> : null}
            <span style={{ ...muted, fontSize: 12 }}>
              {[
                item.durationMs !== null ? fmtDuration(item.durationMs) : '',
                item.steps ? tr.agents.timeline.steps(item.steps) : '',
                item.outputBytes !== null ? tr.agents.timeline.output(fmtKB(item.outputBytes)) : '',
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </Row>
          {item.excerpt ? (
            <Disclosure summary={tr.agents.timeline.outputExcerpt}>
              <MonoBlock text={item.excerpt} maxHeight={240} />
            </Disclosure>
          ) : null}
        </div>
      );
    case 'retrieval_warning':
      return (
        <Notice tone="warn">
          {tr.agents.timeline.retrievalWarning(item.name, item.step, fmtKB(item.bytes), fmtKB(item.threshold), item.via === 'mcp')}
        </Notice>
      );
    case 'evidence_check':
      if (item.mismatched === 0) {
        return (
          <Row>
            <Who name={item.name} role={role(item.agent)} />
            <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.findingsMatch(item.checked)}</span>
          </Row>
        );
      }
      return (
        <Notice tone="warn">
          {tr.agents.timeline.findingsMismatch(
            item.name,
            item.mismatched,
            item.checked,
            item.failures
              .slice(0, 8)
              .map((f) => `${f.path}${f.line ? `:${f.line}` : ''}${f.reason ? ` (${f.reason})` : ''}`)
              .join('\n'),
            item.failures.length - 8
          )}
        </Notice>
      );
    case 'compiler_index': {
      const bad = item.status === 'failed' || item.status === 'timeout';
      const counts = [
        item.packages !== null ? tr.agents.timeline.packages(item.packages) : '',
        item.defs !== null ? tr.agents.timeline.defs(item.defs) : '',
        item.refs !== null ? tr.agents.timeline.refs(item.refs) : '',
        item.unresolved ? tr.agents.timeline.unresolved(item.unresolved) : '',
        item.ms !== null ? fmtDuration(item.ms) : '',
      ].filter(Boolean);
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <strong>{tr.agents.timeline.compilerIndex(item.lang.toUpperCase())}</strong>
            <StatusChip tone={bad ? 'crit' : item.status === 'unavailable' ? 'warn' : 'ok'} title={item.status.toUpperCase()}>
              {tr.agents.indexStatus[item.status.toUpperCase()] ?? item.status.toUpperCase()}
            </StatusChip>
            {counts.length ? <span style={{ ...muted, fontSize: 12 }}>{counts.join(' · ')}</span> : null}
            {item.sha ? <span style={{ ...mono, ...muted, fontSize: 12 }} title={item.sha}>{item.sha.slice(0, 7)}</span> : null}
          </Row>
          {item.error ? <Notice tone={bad ? 'crit' : 'warn'}>{`${item.error}\n${tr.agents.timeline.indexFallback}`}</Notice> : null}
        </div>
      );
    }
    case 'compiler_index_warning':
      return <Notice tone="warn">{tr.agents.timeline.indexCleanup(item.lang.toUpperCase(), item.message || tr.agents.timeline.indexCleanupDefault)}</Notice>;
    case 'wiki_started':
      return (
        <Row>
          <strong>{tr.agents.timeline.wikiStarted}</strong>
          <span style={{ ...muted, fontSize: 12 }}>
            {tr.agents.timeline.wikiStartedInfo(item.pages.length, item.language, item.force)}
          </span>
          {item.sha ? <span style={{ ...mono, ...muted, fontSize: 12 }} title={item.sha}>{item.sha.slice(0, 7)}</span> : null}
        </Row>
      );
    case 'wiki_page_written':
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <strong>{tr.agents.timeline.pageWritten}</strong>
            <span style={{ ...mono, fontSize: 12 }}>{item.page}</span>
            <StatusChip tone={wikiStateTone(item.state)} title={item.state || undefined}>
              {wikiStateLabel(item.state)}
            </StatusChip>
            <span style={{ ...muted, fontSize: 12 }}>
              {[
                item.basisFiles !== null ? tr.agents.timeline.filesRead(item.basisFiles) : '',
                item.citations !== null ? tr.agents.timeline.cited(item.citations) : '',
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </Row>
          {item.unverified.length ? (
            <Notice tone="warn">{tr.agents.timeline.wikiUnverified(item.unverified.length, item.unverified.join('\n'))}</Notice>
          ) : null}
        </div>
      );
    case 'wiki_page_skipped':
      return <Notice tone="warn">{tr.agents.timeline.pageSkipped(item.page, item.reason || tr.agents.timeline.noReason)}</Notice>;
    case 'wiki_finished':
      return (
        <Row>
          <strong>{tr.agents.timeline.wikiFinished}</strong>
          <StatusChip tone={item.failed > 0 && item.written === 0 ? 'crit' : item.failed > 0 ? 'warn' : 'ok'}>{tr.agents.timeline.writtenCount(item.written)}</StatusChip>
          <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.skippedFailed(item.skipped, item.failed)}</span>
        </Row>
      );
    case 'verdict':
      return (
        <Row>
          <strong>{tr.agents.timeline.verdict}</strong>
          <StatusChip tone={decisionStatus(item).tone} title={decisionStatus(item).label}>
            {decisionText(decisionStatus(item).label)}
          </StatusChip>
        </Row>
      );
    case 'jev_gate':
      return (
        <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
          <Row>
            <strong>{tr.agents.timeline.jevGate}</strong>
            <StatusChip tone={jevGateTone(item.gate)} title={item.gate || undefined}>
              {gateLabel(item.gate)}
            </StatusChip>
          </Row>
          {item.reasons.map((r, i) => (
            <div key={i} style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{`- ${r}`}</div>
          ))}
        </div>
      );
    case 'action':
      return (
        <Row>
          <strong>{tr.agents.timeline.actionProposed}</strong>
          <span style={{ ...mono, fontSize: 12 }}>{item.tool}</span>
          <StatusChip tone={actionStatusTone(item.status)} title={item.status || undefined}>
            {actionLabel(item.status)}
          </StatusChip>
          {item.id ? <a href={hrefFor('approvals', [item.id])}>{`#${item.id}`}</a> : null}
        </Row>
      );
    case 'finished':
      return (
        <Row>
          <strong>{tr.agents.timeline.debateFinished}</strong>
          {(() => {
            // debate_finished carries the decision word (APPROVED, REVISION_REQUIRED, ...); older events may carry a job state.
            const decision = decisionWordStatus(item.status);
            if (decision) return <StatusChip tone={decision.tone} title={decision.label}>{decisionText(decision.label)}</StatusChip>;
            return (
              <StatusChip tone={item.status === 'completed' ? 'ok' : item.status === 'failed' ? 'crit' : 'warn'} title={item.status}>
                {tr.agents.jobStatus[item.status] ?? item.status}
              </StatusChip>
            );
          })()}
          {item.consensus !== null ? <span style={{ ...muted, fontSize: 12 }}>{item.consensus ? tr.agents.timeline.consensus : tr.agents.timeline.noConsensus}</span> : null}
          {item.finalRound !== null ? <span style={{ ...muted, fontSize: 12 }}>{tr.agents.timeline.finalRound(item.finalRound)}</span> : null}
        </Row>
      );
    case 'error':
      return <Notice tone="crit">{`${item.name ? `${item.name}: ` : ''}${item.message || tr.agents.timeline.errorDefault}`}</Notice>;
    case 'truncated':
      return <Notice tone="warn">{tr.agents.timeline.truncated}</Notice>;
    default:
      return null;
  }
}

/** Chronological events with round dividers. Follows new events unless the user scrolled up. */
export function Timeline({ run, lanes, jobId = null }: { run: RunState; lanes: Lane[]; jobId?: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const count = run.timeline.length;
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [count]);
  const role = (agent: string) => lanes.find((l) => l.key === agent)?.role ?? '';

  return (
    <div
      ref={ref}
      role="log"
      aria-label={tr.agents.timeline.aria}
      tabIndex={0}
      onScroll={() => {
        const el = ref.current;
        if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
      }}
      style={{ maxHeight: 'min(70vh, 720px)', overflowY: 'auto', minWidth: 0 }}
    >
      <ol style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-4)' }}>
        {run.timeline.map((item) =>
          item.kind === 'round' ? (
            <li
              key={item.seq}
              aria-label={tr.agents.timeline.roundAria(item.round)}
              style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-4)', color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600 }}
            >
              <span style={{ flex: 1, height: 1, background: 'var(--sg-border)' }} />
              <span>{tr.agents.timeline.round(item.round, run.maxRounds)}</span>
              <span style={{ flex: 1, height: 1, background: 'var(--sg-border)' }} />
            </li>
          ) : (
            <li key={item.seq} style={{ display: 'grid', gridTemplateColumns: '72px minmax(0, 1fr)', gap: 'var(--sg-s-4)', alignItems: 'start' }}>
              <time dateTime={item.ts} title={fmtDateTime(item.ts)} style={{ ...mono, ...muted, fontSize: 11, paddingTop: 2 }}>
                {offsetLabel(item.ts, run.startedAt)}
              </time>
              <div style={{ minWidth: 0, fontSize: 13 }}>{itemBody(item, role, jobId)}</div>
            </li>
          )
        )}
      </ol>
    </div>
  );
}

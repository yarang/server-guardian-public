import { StatusChip, StatusDot } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { Card } from '../../components/Card';
import { Row, mono, muted } from '../../components/layout';
import { fmtDuration, roleTone } from '../../lib/debate';
import { fmtBytes } from '../../lib/format';
import { laneActive, laneElapsedMs, laneLabel, laneTone } from '../../lib/agentMonitor';
import type { Lane } from '../../lib/agentMonitor';

function LaneCard({ lane, now }: { lane: Lane; now: number }) {
  const elapsed = laneElapsedMs(lane, now);
  const active = laneActive(lane);
  const tone = laneTone(lane.state);
  const detail: string[] = [];
  if (lane.steps) detail.push(tr.agents.lane.retrievalSteps(lane.steps));
  if (lane.resultsRead || lane.bytesRead) detail.push(tr.agents.lane.thisTurn(lane.resultsRead, fmtBytes(lane.bytesRead)));
  return (
    <Card tone={lane.state === 'failed' ? 'crit' : lane.state === 'builtin' ? 'warn' : undefined} style={{ alignContent: 'start' }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Row gap="var(--sg-s-2)">
          <StatusChip tone={roleTone(lane.role)}>{lane.role || tr.agents.lane.agent}</StatusChip>
          {lane.viaMcp ? <StatusChip tone="info">MCP</StatusChip> : null}
        </Row>
        <Row gap="var(--sg-s-2)">
          {active ? <StatusDot tone={tone} pulse label={laneLabel(lane.state)} /> : null}
          <StatusChip tone={tone} title={lane.state}>
            {laneLabel(lane.state)}
          </StatusChip>
        </Row>
      </Row>
      <div style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{lane.name}</div>
      <div style={{ fontSize: 12, ...muted }}>
        {tr.agents.lane.provider}
        <span style={{ ...mono, color: 'var(--sg-text-secondary)' }}>{lane.provider ?? '-'}</span>
        {lane.resumed && active ? tr.agents.lane.sessionResumed : ''}
      </div>
      <div style={{ fontSize: 13 }} data-testid={`lane-elapsed-${lane.key}`}>
        {elapsed === null ? '-' : active ? tr.agents.lane.seconds(Math.floor(elapsed / 1000)) : fmtDuration(elapsed)}
        {lane.round ? <span style={{ ...muted, fontSize: 12 }}>{tr.agents.lane.roundSuffix(lane.round)}</span> : null}
      </div>
      {detail.length ? <div style={{ ...muted, fontSize: 12 }}>{detail.join(' · ')}</div> : null}
      {lane.totalResults || lane.totalBytes ? (
        <div style={{ ...muted, fontSize: 12 }} data-testid={`lane-total-${lane.key}`}>{tr.agents.lane.runTotal(lane.totalResults, fmtBytes(lane.totalBytes))}</div>
      ) : null}
      {lane.budgetExhausted ? <div style={{ fontSize: 12, color: 'var(--sg-warn)' }}>{tr.agents.lane.budgetUsed}</div> : null}
      {lane.deferred ? <div style={{ fontSize: 12, color: 'var(--sg-warn)' }}>{tr.agents.lane.deferred(lane.deferred)}</div> : null}
      {lane.trimmed ? (
        <div style={{ fontSize: 12, color: 'var(--sg-warn)' }} title={tr.agents.lane.trimmedTitle(lane.trimmedChars)}>
          {tr.agents.lane.trimmed(lane.trimmed)}
        </div>
      ) : null}
      {lane.actionRepair ? (
        <div style={{ fontSize: 12, color: lane.actionRepair.fixed < lane.actionRepair.invalid ? 'var(--sg-warn)' : undefined, ...(lane.actionRepair.fixed < lane.actionRepair.invalid ? {} : muted) }}>
          {tr.agents.lane.actionsRepaired(lane.actionRepair.fixed, lane.actionRepair.invalid)}
        </div>
      ) : null}
      {lane.formatRetry ? (
        <div style={{ fontSize: 12, color: 'var(--sg-warn)' }}>
          {tr.agents.lane.omitted(lane.formatRetry.length ? lane.formatRetry.join('/') : tr.agents.timeline.omittedDefault)}
        </div>
      ) : null}
      {lane.pseudoCalls ? <div style={{ fontSize: 12, color: 'var(--sg-warn)' }}>{tr.agents.lane.fakeCalls(lane.pseudoCalls)}</div> : null}
      {lane.unverifiedCitations.length ? (
        <div style={{ fontSize: 12, color: 'var(--sg-warn)' }} title={lane.unverifiedCitations.join('\n')}>
          {tr.agents.lane.unverifiedCitations(lane.unverifiedCitations.length, lane.citationAsked)}
        </div>
      ) : null}
      {lane.noCodeRead ? <div style={{ fontSize: 12, color: 'var(--sg-warn)' }}>{tr.agents.lane.noCodeRead}</div> : null}
      {lane.fallbacks.map((f) => (
        <div
          key={f.seq}
          title={f.error}
          style={{ fontSize: 12, padding: 'var(--sg-s-2) var(--sg-s-3)', border: '1px solid var(--sg-warn-border)', background: 'var(--sg-warn-bg)', borderRadius: 'var(--sg-r-md)', overflowWrap: 'anywhere' }}
        >
          {tr.agents.lane.providerFailed(f.provider || tr.agents.lane.providerFallback, f.error ? f.error.slice(0, 120) : '')}
        </div>
      ))}
    </Card>
  );
}

/** One card per team member: Expert, critics, Judge. */
export function LaneCards({ lanes, now }: { lanes: Lane[]; now: number }) {
  if (lanes.length === 0) return null;
  return (
    <section aria-label={tr.agents.lane.aria} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--sg-s-5)', minWidth: 0 }}>
      {lanes.map((l) => (
        <LaneCard key={l.key} lane={l} now={now} />
      ))}
    </section>
  );
}

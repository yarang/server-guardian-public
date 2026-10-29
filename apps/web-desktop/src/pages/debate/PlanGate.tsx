import { StatusChip } from '@server-guardian/ui';
import { Caption } from '../../components/Card';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { tr } from '../../i18n';
import type { PlanGate as Gate, PlanItem, PlanVote } from '../../types';

const P = () => tr.debate.plans;

function voteLabel(v: PlanVote): { tone: 'ok' | 'crit' | 'warn' | 'idle'; text: string } {
  if (v.vote === 'approve') return { tone: 'ok', text: P().voteApprove };
  if (v.vote === 'abstain') return { tone: 'idle', text: P().voteAbstain };
  return v.counted ? { tone: 'crit', text: P().voteObjection } : { tone: 'warn', text: P().voteAdvice };
}

function Reasons({ items }: { items: string[] }) {
  if (items.length === 0) return <div style={{ ...muted, fontSize: 12 }}>{P().gateNoReason}</div>;
  return (
    <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-5)', display: 'grid', gap: 2 }}>
      {items.map((r, i) => (
        <li key={i} style={{ overflowWrap: 'anywhere' }}>
          {r}
        </li>
      ))}
    </ul>
  );
}

/** Where the plan stands with the gate: ran by itself, refused at the start, released, or waiting for a person. */
function Verdict({ gate, open }: { gate: Gate; open: boolean }) {
  const run = gate.auto_run;
  if (run?.started_at) {
    return (
      <Stack gap="var(--sg-s-2)">
        <Notice tone={run.error ? 'warn' : 'ok'}>{run.finished_at || run.error ? P().gateAutoRan(run.actor ?? '') : P().gateAutoRunning}</Notice>
        {run.error ? <Notice tone="warn">{P().gateAutoError(run.error)}</Notice> : null}
      </Stack>
    );
  }
  // Once a person ran or rejected the plan, "released" and "waiting" would only be stale.
  if (!open) return null;
  if (gate.release_refused) {
    return (
      <Notice tone="warn">
        <div>{P().gateRefused}</div>
        <Reasons items={gate.release_refused.reasons} />
      </Notice>
    );
  }
  if (gate.auto) return <Notice tone="info">{P().gateReleased}</Notice>;
  return (
    <Stack gap="var(--sg-s-2)">
      <Notice tone="info">
        <div>{P().gateWaiting}</div>
        <Reasons items={gate.reasons} />
      </Notice>
      {gate.would_auto ? <div style={{ ...muted, fontSize: 12 }}>{P().gateShadow}</div> : null}
    </Stack>
  );
}

/** The critics' votes and the batch gate's decision for a plan (docs/design/batch-execution.md, B4). Nothing for a plan without a vote pass. */
export function PlanGate({ plan, open }: { plan: PlanItem; open: boolean }) {
  const gate = plan.gate;
  const votes = plan.votes ?? [];
  if (!gate && votes.length === 0) return null;
  return (
    <Stack gap="var(--sg-s-3)">
      <Caption>{P().gateTitle}</Caption>
      {gate ? <Verdict gate={gate} open={open} /> : null}
      {gate?.notes?.length ? <div style={{ ...muted, fontSize: 12 }}>{`${P().gateNotes}: ${gate.notes.join(' · ')}`}</div> : null}
      <Caption>{votes.length > 0 ? P().gateVotes(votes.length) : P().gateNoVotes}</Caption>
      {votes.map((v) => {
        const label = voteLabel(v);
        return (
          <Stack key={v.critic} gap="var(--sg-s-1)">
            <Row>
              <span style={mono}>{v.critic}</span>
              <StatusChip tone={label.tone}>{label.text}</StatusChip>
            </Row>
            {v.reason ? <div style={{ overflowWrap: 'anywhere', lineHeight: 1.5 }}>{v.reason}</div> : null}
            {v.note ? <div style={{ ...muted, fontSize: 12 }}>{v.note}</div> : null}
            {v.cites.length > 0 ? (
              <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
                {P().voteCites}: <span style={mono}>{v.cites.join(' · ')}</span>
              </div>
            ) : null}
          </Stack>
        );
      })}
      {plan.plan_hash ? (
        <div style={{ ...muted, fontSize: 12 }}>
          {P().gatePlanHash}: <span style={mono}>{plan.plan_hash}</span>
        </div>
      ) : null}
    </Stack>
  );
}

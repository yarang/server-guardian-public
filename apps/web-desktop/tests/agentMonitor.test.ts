import { describe, expect, it } from 'vitest';
import {
  finalizeRun, initialRunState, laneElapsedMs, offsetLabel, reduceEvent, reduceEvents, requestLabel, runElapsedMs, runRetrieval, sortRuns,
} from '../src/lib/agentMonitor';
import { ko } from '../src/i18n/ko';
import type { DebateEvent } from '../src/types';

let seq = 0;
function ev(type: string, extra: Record<string, unknown> = {}, ts = '2026-10-08T05:21:00.000Z'): DebateEvent {
  seq += 1;
  return { seq, ts, thread_id: 'thread_x', type, ...extra } as DebateEvent;
}
const at = (s: number) => `2026-10-08T05:21:${String(s).padStart(2, '0')}.000Z`;
const expert = { round: 1, role: 'expert', agent: 'expert', agent_name: 'Expert' };
const critic = { round: 1, role: 'critic', agent: 'critic_redteam', agent_name: 'Red Team' };

function start(): DebateEvent[] {
  seq = 0;
  return [
    ev('debate_started', {
      data: {
        task: 'find broken APIs', project: 'server-guardian', team: 'cross', max_rounds: 2,
        members: [
          { role: 'judge', agent: 'judge', agent_name: 'Judge', providers: ['claude'] },
          { role: 'critic', agent: 'critic_redteam', agent_name: 'Red Team', providers: ['codex', 'claude'] },
          { role: 'expert', agent: 'expert', agent_name: 'Expert', providers: ['claude', 'agy'] },
        ],
        context: { sha: 'abc1234def', files: 12 },
        host: 'hub-1',
      },
    }, at(0)),
    ev('round_started', { round: 1 }, at(1)),
  ];
}

describe('retrieval cost', () => {
  const served = (n: number, bytes: number, step: number) =>
    ev('retrieval_served', { ...expert, data: { step, results: Array.from({ length: n }, (_, i) => ({ op: 'read', target: `f${i}`, ok: true, bytes })) } });

  it('sums served results live per turn, per lane and per run; failed reads do not count', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    run = reduceEvent(run, served(3, 100, 1));
    run = reduceEvent(run, ev('retrieval_served', { ...expert, data: { step: 2, results: [{ op: 'read', target: 'x', ok: false, bytes: 0, error: 'excluded' }, { op: 'read', target: 'y', ok: true, bytes: 50 }] } }));
    expect(run.totals).toMatchObject({ results: 4, bytesRead: 350 });
    expect(run.lanes[0]).toMatchObject({ resultsRead: 4, totalResults: 4, totalBytes: 350 });
    // A new turn resets the turn counters, never the totals.
    run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'claude' } }));
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    run = reduceEvent(run, served(1, 10, 1));
    expect(run.lanes[0]).toMatchObject({ resultsRead: 1, totalResults: 5, totalBytes: 360 });
    expect(runRetrieval(run)).toEqual({ results: 5, bytes: 360 });
  });

  it('debate_finished retrieval overrides the live sums, per lane via by_agent', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    run = reduceEvent(run, served(2, 10, 1));
    run = reduceEvent(run, ev('debate_finished', { data: { status: 'completed', retrieval: { results: 176, bytes: 2097152, by_agent: { expert: { results: 100, bytes: 1000, steps: 7 } } } } }));
    expect(run.retrieval).toEqual({ results: 176, bytes: 2097152 });
    expect(runRetrieval(run)).toEqual({ results: 176, bytes: 2097152 });
    expect(run.lanes.find((l) => l.key === 'expert')).toMatchObject({ totalResults: 100, totalBytes: 1000 });
    // Lanes the event does not name keep their live sums.
    expect(run.lanes.find((l) => l.key === 'judge')).toMatchObject({ totalResults: 0 });
  });

  it('keeps the live sums when debate_finished carries no retrieval (old runs)', () => {
    let run = reduceEvents(initialRunState(), start());
    expect(runRetrieval(run)).toBeNull();
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    run = reduceEvent(run, served(2, 10, 1));
    run = reduceEvent(run, ev('debate_finished', { data: { status: 'completed' } }));
    expect(run.retrieval).toBeNull();
    expect(runRetrieval(run)).toEqual({ results: 2, bytes: 20 });
  });
});

describe('reduceEvent lanes', () => {
  it('builds lanes from debate_started ordered expert, critics, judge', () => {
    const run = reduceEvents(initialRunState(), start());
    expect(run.lanes.map((l) => l.key)).toEqual(['expert', 'critic_redteam', 'judge']);
    expect(run).toMatchObject({ task: 'find broken APIs', project: 'server-guardian', team: 'cross', sha: 'abc1234def', host: 'hub-1', maxRounds: 2, round: 1, status: 'running', startedAt: at(0) });
    expect(run.lanes.every((l) => l.state === 'idle')).toBe(true);
  });

  it('walks idle -> running -> retrieving -> running -> done with steps and bytes', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', { ...expert, data: { resumed: true, prompt_bytes: 2048 } }, at(2)));
    let lane = run.lanes[0];
    expect(lane).toMatchObject({ state: 'running', provider: 'claude', startedAt: at(2), resumed: true });

    run = reduceEvent(run, ev('retrieval_requested', { ...expert, data: { step: 1, requests: [{ op: 'read', target: 'apps/agent/main.go:100-180' }] } }, at(3)));
    expect(run.lanes[0].state).toBe('retrieving');
    run = reduceEvent(run, ev('retrieval_served', {
      ...expert,
      data: { step: 1, turn_bytes: 5000, results: [{ op: 'read', target: 'apps/agent/main.go:100-180', ok: true, bytes: 5000, truncated: false }] },
    }, at(4)));
    lane = run.lanes[0];
    expect(lane).toMatchObject({ state: 'running', steps: 1, bytesRead: 5000 });
    expect(run.totals).toEqual({ steps: 1, bytesRead: 5000, results: 1 });

    run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'claude', duration_ms: 9000, steps: 1, output_bytes: 3000, excerpt: 'hello', fallback: false } }, at(11)));
    lane = run.lanes[0];
    expect(lane).toMatchObject({ state: 'done', provider: 'claude', startedAt: null, durationMs: 9000, outputBytes: 3000 });
  });

  it('marks a builtin answer as builtin, not done', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert));
    run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'builtin', fallback: true, duration_ms: 5 } }));
    expect(run.lanes[0].state).toBe('builtin');
  });

  it('records provider fallbacks and moves the current provider on', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', critic));
    expect(run.lanes[1].provider).toBe('codex');
    run = reduceEvent(run, ev('provider_failed', { ...critic, data: { provider: 'codex', error: 'rate limited' } }));
    expect(run.lanes[1].provider).toBe('claude');
    expect(run.lanes[1].fallbacks).toHaveLength(1);
    expect(run.fallbacks[0]).toMatchObject({ provider: 'codex', error: 'rate limited', agentName: 'Red Team' });
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'provider_failed', provider: 'codex' });
  });

  it('resets idle lanes on a new round and keeps in-flight ones', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert));
    run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'claude', duration_ms: 1 } }));
    run = reduceEvent(run, ev('agent_started', critic));
    run = reduceEvent(run, ev('round_started', { round: 2 }));
    expect(run.round).toBe(2);
    expect(run.lanes.find((l) => l.key === 'expert')?.state).toBe('idle');
    expect(run.lanes.find((l) => l.key === 'critic_redteam')?.state).toBe('running');
    expect(run.timeline.filter((t) => t.kind === 'round')).toHaveLength(2);
  });

  it('creates a lane for an agent missing from debate_started', () => {
    seq = 0;
    const run = reduceEvents(initialRunState(), [ev('agent_started', { round: 3, role: 'critic', agent: 'late', agent_name: 'Late Critic' })]);
    expect(run.lanes[0]).toMatchObject({ key: 'late', name: 'Late Critic', state: 'running' });
  });

  it('fails the lane on an agent error and a run-level error stays out of the lanes', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert));
    run = reduceEvent(run, ev('error', { ...expert, data: { message: 'all providers failed' } }));
    expect(run.lanes[0].state).toBe('failed');
    run = reduceEvent(run, ev('error', { data: { message: 'engine crashed' } }));
    expect(run.errors).toEqual(['engine crashed']);
  });

  it('fails lanes still in flight when the debate finishes', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert));
    run = reduceEvent(run, ev('debate_finished', { data: { status: 'failed', consensus: false, final_round: 1 } }, at(30)));
    expect(run).toMatchObject({ status: 'failed', consensus: false, finalRound: 1, finishedAt: at(30) });
    expect(run.lanes[0].state).toBe('failed');
  });
});

describe('reduceEvent timeline', () => {
  it('pairs a served result with its request by agent and step', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('retrieval_requested', { ...expert, data: { step: 1, requests: [{ op: 'list', target: 'apps' }, { op: 'read', target: 'x.go' }] } }));
    run = reduceEvent(run, ev('retrieval_requested', { ...critic, data: { step: 1, requests: [{ op: 'grep', target: 'foo' }] } }));
    run = reduceEvent(run, ev('retrieval_served', {
      ...expert,
      data: { step: 1, turn_bytes: 10, results: [{ op: 'list', target: 'apps', ok: true, bytes: 10 }, { op: 'read', target: 'x.go', ok: false, bytes: 0, error: 'excluded' }] },
    }));
    const items = run.timeline.filter((t) => t.kind === 'retrieval');
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ agent: 'expert', results: [{ ok: true }, { error: 'excluded', ok: false }] });
    expect(items[1]).toMatchObject({ agent: 'critic_redteam', results: null });
    expect(run.totals.bytesRead).toBe(10);
  });

  it('tags retrieval items and lanes served through MCP; older events count as repo blocks', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('retrieval_requested', { ...expert, data: { step: 1, via: 'mcp', requests: [{ op: 'read', target: 'a.go' }] } }));
    run = reduceEvent(run, ev('retrieval_requested', { ...critic, data: { step: 1, requests: [{ op: 'read', target: 'b.go' }] } }));
    run = reduceEvent(run, ev('retrieval_served', { ...expert, data: { step: 1, via: 'mcp', turn_bytes: 4, results: [{ op: 'read', target: 'a.go', ok: true, bytes: 4 }] } }));
    run = reduceEvent(run, ev('retrieval_served', { ...critic, data: { step: 1, turn_bytes: 4, results: [{ op: 'read', target: 'b.go', ok: true, bytes: 4 }] } }));
    const items = run.timeline.filter((t) => t.kind === 'retrieval');
    expect(items.map((i) => (i.kind === 'retrieval' ? i.via : ''))).toEqual(['mcp', 'block']);
    expect(run.lanes.find((l) => l.key === 'expert')?.viaMcp).toBe(true);
    expect(run.lanes.find((l) => l.key === 'critic_redteam')?.viaMcp).toBe(false);
    // The tag is per turn: the next turn of the member clears it.
    run = reduceEvent(run, ev('round_started', { round: 2 }));
    run = reduceEvent(run, ev('agent_started', { ...expert, round: 2, data: {} }));
    expect(run.lanes.find((l) => l.key === 'expert')?.viaMcp).toBe(false);
  });

  it('keeps node checks of ops debates apart from code reads', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('retrieval_requested', { ...expert, data: { step: 1, via: 'ops', requests: [{ op: 'disk_usage', target: 'n1' }] } }));
    run = reduceEvent(run, ev('retrieval_served', { ...expert, data: { step: 1, via: 'ops', turn_bytes: 40, results: [{ op: 'disk_usage', target: 'n1', ok: true, bytes: 40 }] } }));
    const items = run.timeline.filter((t) => t.kind === 'retrieval');
    expect(items.map((i) => (i.kind === 'retrieval' ? i.via : ''))).toEqual(['ops']);
    expect(run.lanes.find((l) => l.key === 'expert')?.viaMcp).toBe(false);
  });

  it('adds a retrieval item when a served event has no request, and flags the budget', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('retrieval_served', { ...expert, data: { step: 2, results: [{ op: 'read', target: 'a', ok: true, bytes: 4, truncated: true }] } }));
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'retrieval', step: 2, requests: [{ op: 'read', target: 'a' }] });
    run = reduceEvent(run, ev('retrieval_exhausted', { ...expert, data: { steps: 8, turn_bytes: 400000 } }));
    expect(run.lanes[0].budgetExhausted).toBe(true);
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'exhausted', steps: 8 });
  });

  it('collects verdict, jev gate, actions and truncation', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('verdict', { data: { approved: true, unverified: false } }));
    run = reduceEvent(run, ev('jev_gate', { data: { gate: 'PASSED', reasons: ['ok'] } }));
    run = reduceEvent(run, ev('action_proposed', { data: { id: 7, tool: 'jev_mcp:apply_patch', status: 'PROPOSED' } }));
    run = reduceEvent(run, ev('events_truncated'));
    expect(run.verdict).toEqual({ approved: true, unverified: false });
    expect(run.gate).toEqual({ gate: 'PASSED', reasons: ['ok'] });
    expect(run.actions).toEqual([{ id: '7', tool: 'jev_mcp:apply_patch', status: 'PROPOSED' }]);
    expect(run.truncated).toBe(true);
  });

  it('ignores replayed seqs and unknown types but advances the cursor', () => {
    let run = reduceEvents(initialRunState(), start());
    const before = run.timeline.length;
    expect(reduceEvent(run, { seq: 1, ts: at(0), type: 'round_started', round: 5 } as DebateEvent)).toBe(run);
    run = reduceEvent(run, { seq: 99, ts: at(5), type: 'something_new' } as unknown as DebateEvent);
    expect(run.lastSeq).toBe(99);
    expect(run.timeline).toHaveLength(before);
  });

  it('tolerates events with no data', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert));
    run = reduceEvent(run, ev('retrieval_requested', expert));
    run = reduceEvent(run, ev('agent_finished', expert));
    expect(run.lanes[0].state).toBe('done');
  });
});

describe('time and ordering helpers', () => {
  it('counts lane elapsed live while in flight and uses the recorded duration after', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert, at(2)));
    expect(laneElapsedMs(run.lanes[0], Date.parse(at(12)))).toBe(10000);
    run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'claude', duration_ms: 8000 } }, at(10)));
    expect(laneElapsedMs(run.lanes[0], Date.parse(at(59)))).toBe(8000);
    expect(laneElapsedMs(run.lanes[1], Date.parse(at(59)))).toBeNull();
  });

  it('measures run elapsed to the end once finished', () => {
    let run = reduceEvents(initialRunState(), start());
    expect(runElapsedMs(run, Date.parse(at(20)))).toBe(20000);
    run = reduceEvent(run, ev('debate_finished', { data: { status: 'completed' } }, at(30)));
    expect(runElapsedMs(run, Date.parse(at(50)))).toBe(30000);
  });

  it('finalizeRun settles a run whose job ended without debate_finished', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', expert, at(2)));
    const done = finalizeRun(run, 'cancelled');
    expect(done.status).toBe('cancelled');
    expect(done.lanes[0].state).toBe('failed');
    const finished = reduceEvent(run, ev('debate_finished', { data: { status: 'completed' } }));
    expect(finalizeRun(finished, 'failed')).toBe(finished);
  });

  it('labels requests and offsets', () => {
    expect(requestLabel({ op: 'read', target: 'apps/agent/main.go:100-180' })).toBe('read apps/agent/main.go:100-180');
    expect(offsetLabel(at(5), at(0))).toBe('+5초');
    expect(offsetLabel(at(59), '2026-10-08T05:19:56.000Z')).toBe('+2분 03초');
  });

  it('sorts running first then newest first without mutating', () => {
    const jobs = [
      { id: 'a', status: 'completed', started_at: '2026-10-08T05:00:00Z' },
      { id: 'b', status: 'running', started_at: '2026-10-08T04:00:00Z' },
      { id: 'c', status: 'failed', started_at: '2026-10-08T06:00:00Z' },
      { id: 'd', status: 'running', started_at: '2026-10-08T04:30:00Z' },
    ];
    expect(sortRuns(jobs).map((j) => j.id)).toEqual(['d', 'b', 'c', 'a']);
    expect(jobs[0].id).toBe('a');
  });
});

describe('retrieval hardening events', () => {
  const started = (): ReturnType<typeof initialRunState> => {
    let run = reduceEvents(initialRunState(), start());
    return reduceEvent(run, ev('agent_started', { ...expert, data: {} }, at(2)));
  };

  it('marks deferred results and counts them on the lane', () => {
    let run = started();
    run = reduceEvent(run, ev('retrieval_requested', { ...expert, data: { step: 1, requests: [{ op: 'read', target: 'a.go' }, { op: 'read', target: 'b.go' }] } }));
    run = reduceEvent(run, ev('retrieval_served', {
      ...expert,
      data: {
        step: 1, turn_bytes: 10,
        results: [
          { op: 'read', target: 'a.go', ok: true, bytes: 10 },
          { op: 'read', target: 'b.go', ok: false, bytes: 0, error: 'deferred', deferred: true },
        ],
      },
    }));
    const item = run.timeline.at(-1) as Extract<(typeof run.timeline)[number], { kind: 'retrieval' }>;
    expect(item.results?.map((r) => r.deferred === true)).toEqual([false, true]);
    expect(run.lanes[0].deferred).toBe(1);
    // A new turn starts the count over.
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    expect(run.lanes[0].deferred).toBe(0);
  });

  it('records reply_trimmed on the timeline and lane', () => {
    let run = started();
    run = reduceEvent(run, ev('reply_trimmed', { ...expert, data: { step: 1, discarded_chars: 6400, blocks: 3 } }));
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'reply_trimmed', step: 1, discardedChars: 6400, blocks: 3 });
    expect(run.lanes[0]).toMatchObject({ trimmed: 1, trimmedChars: 6400 });
  });

  it('records format_retry on the judge lane and pseudo_tool_call per turn', () => {
    let run = started();
    const judge = { round: 1, role: 'judge', agent: 'judge', agent_name: 'Judge' };
    run = reduceEvent(run, ev('format_retry', { ...judge, data: { missing: ['verdict', 'json'] } }));
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'format_retry', missing: ['verdict', 'json'] });
    expect(run.lanes.find((l) => l.key === 'judge')?.formatRetry).toEqual(['verdict', 'json']);

    run = reduceEvent(run, ev('pseudo_tool_call', { ...expert, data: { step: 2, tools: ['glob', 'ls'], discarded_chars: 300 } }));
    run = reduceEvent(run, ev('pseudo_tool_call', { ...expert, data: { step: 3 } }));
    expect(run.timeline.at(-2)).toMatchObject({ kind: 'pseudo_tool_call', step: 2, tools: ['glob', 'ls'], discardedChars: 300 });
    expect(run.timeline.at(-1)).toMatchObject({ tools: [], discardedChars: 0 });
    expect(run.lanes[0].pseudoCalls).toBe(2);
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    expect(run.lanes[0].pseudoCalls).toBe(0);
    run = reduceEvent(run, ev('agent_started', { ...judge, data: {} }));
    expect(run.lanes.find((l) => l.key === 'judge')?.formatRetry).toBeNull();
  });

  it('records action_repair on the timeline and judge lane, reset per turn', () => {
    let run = started();
    const judge = { round: 1, role: 'judge', agent: 'judge', agent_name: 'Judge' };
    run = reduceEvent(run, ev('action_repair', { ...judge, data: { invalid: [{ index: 0, tool: 'apply_patch', error: 'old text not unique' }, { index: 2, tool: 'apply_patch', error: 'file not found' }], fixed: 1 } }));
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'action_repair', fixed: 1, invalid: [{ index: 0, tool: 'apply_patch', error: 'old text not unique' }, { index: 2 }] });
    expect(run.lanes.find((l) => l.key === 'judge')?.actionRepair).toEqual({ invalid: 2, fixed: 1 });
    run = reduceEvent(run, ev('agent_started', { ...judge, data: {} }));
    expect(run.lanes.find((l) => l.key === 'judge')?.actionRepair).toBeNull();
  });

  it('flags unverified citations until a clean check clears them', () => {
    let run = started();
    run = reduceEvent(run, ev('citation_check', { ...expert, data: { cited: 3, unverified: ['a.go:10', 'b.go'], asked: false } }));
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'citation_check', cited: 3, unverified: ['a.go:10', 'b.go'], asked: false });
    expect(run.lanes[0]).toMatchObject({ unverifiedCitations: ['a.go:10', 'b.go'], citationAsked: false });
    run = reduceEvent(run, ev('citation_check', { ...expert, data: { cited: 3, unverified: [], asked: true } }));
    expect(run.lanes[0].unverifiedCitations).toEqual([]);
  });

  it('flags No code read for an agent that finished with 0 steps in a 0-file debate only', () => {
    const finish = (files: number, steps: number) => {
      seq = 0;
      const evs = start();
      (evs[0] as { data: { context: { files: number } } }).data.context.files = files;
      let run = reduceEvents(initialRunState(), evs);
      run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
      run = reduceEvent(run, ev('agent_finished', { ...expert, data: { provider: 'claude', steps } }));
      return run.lanes[0].noCodeRead;
    };
    expect(finish(0, 0)).toBe(true);
    expect(finish(0, 2)).toBe(false);
    expect(finish(12, 0)).toBe(false);
  });
});

describe('P2.x events in the timeline', () => {
  it('shows retrieval_warning, evidence_check and the compiler index as concise items', () => {
    let run = reduceEvents(initialRunState(), start());
    run = reduceEvent(run, ev('agent_started', { ...expert, data: {} }));
    run = reduceEvent(run, ev('compiler_index', { data: { lang: 'go', sha: 'abc1234def', status: 'built', packages: 4, defs: 120, refs: 900, unresolved: 3, ms: 2100, error: null } }));
    run = reduceEvent(run, ev('compiler_index_warning', { data: { lang: 'go', sha: 'abc1234def', message: 'worktree remove failed' } }));
    run = reduceEvent(run, ev('retrieval_warning', { ...expert, data: { step: 2, bytes: 3_000_000, threshold: 2_097_152, via: 'mcp' } }));
    run = reduceEvent(run, ev('evidence_check', {
      ...expert,
      data: { checked: 2, mismatched: 1, items: [{ path: 'a.go', line: 10, ok: true, reason: 'verified' }, { path: 'b.go', line: 5, ok: false, reason: 'line not shown' }] },
    }));
    const kinds = run.timeline.map((t) => t.kind);
    expect(kinds.slice(-4)).toEqual(['compiler_index', 'compiler_index_warning', 'retrieval_warning', 'evidence_check']);
    expect(run.timeline.find((t) => t.kind === 'compiler_index')).toMatchObject({ lang: 'go', status: 'built', packages: 4, defs: 120, refs: 900, unresolved: 3, ms: 2100, error: '' });
    expect(run.timeline.find((t) => t.kind === 'retrieval_warning')).toMatchObject({ step: 2, bytes: 3_000_000, threshold: 2_097_152, via: 'mcp', agent: 'expert' });
    // Only the items that did not match are kept.
    expect(run.timeline.find((t) => t.kind === 'evidence_check')).toMatchObject({ checked: 2, mismatched: 1, failures: [{ path: 'b.go', line: '5', reason: 'line not shown' }] });
    // They do not change lane state or the run status.
    expect(run.lanes.find((l) => l.key === 'expert')?.state).toBe('running');
    expect(run.status).toBe('running');
  });

  it('tolerates missing data', () => {
    let run = initialRunState();
    for (const type of ['retrieval_warning', 'evidence_check', 'compiler_index', 'compiler_index_warning']) run = reduceEvent(run, ev(type));
    expect(run.timeline).toHaveLength(4);
    expect(run.timeline.find((t) => t.kind === 'evidence_check')).toMatchObject({ checked: 0, mismatched: 0, failures: [] });
  });
});

describe('code wiki build events', () => {
  const writer = { round: 1, role: 'Domain Expert', agent: 'expert', agent_name: 'Expert' };

  function build(): DebateEvent[] {
    seq = 0;
    return [
      ev('wiki_build_started', { thread_id: 'wiki_20261012_a1', data: { project: 'server-guardian', team: 'default', sha: 'abc1234def', pages: ['apps-agent', 'cli-llm-agent'], language: 'Korean', force: false } }, at(0)),
      ev('agent_started', { ...writer, data: { prompt_bytes: 2048 } }, at(1)),
      ev('agent_finished', { ...writer, data: { provider: 'claude', duration_ms: 9000, steps: 6 } }, at(10)),
      ev('wiki_page_written', { data: { page: 'apps-agent', state: 'fresh', basis_files: 8, citations: 5, unverified: [] } }, at(11)),
      ev('wiki_page_skipped', { data: { page: 'cli-llm-agent', reason: 'llm_fallback: no provider answered' } }, at(12)),
    ];
  }

  it('turns a build into a code_wiki run with a page tally', () => {
    let run = reduceEvents(initialRunState(), build());
    expect(run).toMatchObject({ kind: 'code_wiki', status: 'running', project: 'server-guardian', sha: 'abc1234def', task: ko.wiki.jobLabel });
    expect(run.wiki).toMatchObject({
      pages: ['apps-agent', 'cli-llm-agent'], language: 'Korean', force: false, finished: null,
      written: [{ page: 'apps-agent', state: 'fresh', basisFiles: 8, citations: 5, unverified: [] }],
      skipped: [{ page: 'cli-llm-agent', reason: 'llm_fallback: no provider answered' }],
    });
    expect(run.timeline.map((t) => t.kind)).toEqual(['wiki_started', 'agent_started', 'agent_finished', 'wiki_page_written', 'wiki_page_skipped']);
    // The expert lane is the build's one agent.
    expect(run.lanes.map((l) => [l.key, l.state])).toEqual([['expert', 'done']]);

    run = reduceEvent(run, ev('wiki_build_finished', { data: { written: 1, skipped: 1, failed: 1 } }, at(13)));
    expect(run).toMatchObject({ status: 'completed', finishedAt: at(13) });
    expect(run.wiki?.finished).toEqual({ written: 1, skipped: 1, failed: 1 });
    expect(run.timeline.at(-1)).toMatchObject({ kind: 'wiki_finished', written: 1, skipped: 1, failed: 1 });
  });

  it('a build that wrote nothing and failed is a failed run; skips alone are not', () => {
    const failed = reduceEvent(reduceEvents(initialRunState(), build()), ev('wiki_build_finished', { data: { written: 0, skipped: 2, failed: 2 } }));
    expect(failed.status).toBe('failed');
    const kept = reduceEvent(reduceEvents(initialRunState(), build()), ev('wiki_build_finished', { data: { written: 0, skipped: 2, failed: 0 } }));
    expect(kept.status).toBe('completed');
  });

  it('settles an agent still in flight when the build ends, and keeps unverified citations', () => {
    seq = 0;
    let run = reduceEvents(initialRunState(), [
      ev('wiki_build_started', { data: { project: 'p', pages: ['a'] } }),
      ev('agent_started', { ...writer, data: {} }),
      ev('wiki_page_written', { data: { page: 'a', state: 'broken', basis_files: 1, citations: 1, unverified: ['x.go:9'] } }),
      ev('wiki_build_finished', { data: { written: 1, skipped: 0, failed: 0 } }),
    ]);
    expect(run.lanes[0].state).toBe('failed');
    expect(run.wiki?.written[0]).toMatchObject({ state: 'broken', unverified: ['x.go:9'] });
    // finalizeRun leaves a finished build alone.
    expect(finalizeRun(run, 'cancelled')).toBe(run);
  });

  it('a cancelled build keeps what it wrote and takes the job status', () => {
    const run = finalizeRun(reduceEvents(initialRunState(), build()), 'cancelled');
    expect(run.status).toBe('cancelled');
    expect(run.wiki?.written).toHaveLength(1);
    expect(run.lanes[0].state).toBe('done');
  });
});

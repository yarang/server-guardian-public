import type { Tone } from '@server-guardian/ui';
import { tr } from '../i18n';
import type { DebateEvent, DebateEventMember, RetrievalRequest, RetrievalResult, RetrievalVia } from '../types';

// Pure fold of the debate event stream into what the Agents monitor shows.

export type LaneState = 'idle' | 'running' | 'retrieving' | 'done' | 'failed' | 'builtin';

export interface FallbackWarning {
  seq: number;
  round: number | null;
  agent: string;
  agentName: string;
  provider: string;
  error: string;
}

export interface Lane {
  /** Session key (`agent` of the events). */
  key: string;
  role: string;
  name: string;
  /** Providers in fallback order, from `debate_started`. */
  providers: string[];
  state: LaneState;
  /** Provider answering now (running) or that answered last (done). */
  provider: string | null;
  /** `ts` of the latest `agent_started` while the turn is in flight, else null. */
  startedAt: string | null;
  /** Duration of the last finished turn. */
  durationMs: number | null;
  round: number | null;
  resumed: boolean;
  /** Retrieval steps and bytes served in the current (or last) turn. */
  steps: number;
  bytesRead: number;
  /** Results read (served ok) in the current (or last) turn. */
  resultsRead: number;
  /** Run totals for this lane; `debate_finished.retrieval.by_agent` replaces them when present. */
  totalResults: number;
  totalBytes: number;
  outputBytes: number | null;
  budgetExhausted: boolean;
  /** Retrieval requests held back (`deferred`) in the current turn. */
  deferred: number;
  /** `reply_trimmed` events in the current turn, and the characters they discarded. */
  trimmed: number;
  trimmedChars: number;
  /** Latest `citation_check` of the turn: citations never served to this agent; empty when all verified. */
  unverifiedCitations: string[];
  /** The agent was already asked about the unverified citations. */
  citationAsked: boolean;
  /** Finished with no retrieval steps in a debate that had no code files in its context (fabrication signal). */
  noCodeRead: boolean;
  /** Judge was asked once more for missing parts (`verdict` / `json`) in the current turn; null when not. */
  formatRetry: string[] | null;
  /** Judge actions that failed validation and how many the repair turn fixed; null when no repair happened. */
  actionRepair: { invalid: number; fixed: number } | null;
  /** Fake tool-call responses (`pseudo_tool_call`) in the current turn. */
  pseudoCalls: number;
  /** Providers skipped in the current turn. */
  fallbacks: FallbackWarning[];
  /** The member read code through MCP tools in the current (or last) turn. */
  viaMcp: boolean;
}

export type TimelineItem =
  | { kind: 'run_started'; seq: number; ts: string; members: DebateEventMember[]; sha: string; files: number | null; maxRounds: number | null }
  | { kind: 'round'; seq: number; ts: string; round: number }
  | { kind: 'agent_started'; seq: number; ts: string; round: number | null; agent: string; name: string; role: string; resumed: boolean; promptBytes: number | null; promptSaved: boolean; promptTruncated: boolean }
  | {
      kind: 'retrieval';
      seq: number;
      ts: string;
      round: number | null;
      agent: string;
      name: string;
      step: number;
      requests: RetrievalRequest[];
      /** Null until `retrieval_served` arrives. */
      results: RetrievalResult[] | null;
      turnBytes: number | null;
      /** Served through MCP tools rather than repo blocks. */
      via: RetrievalVia;
    }
  | { kind: 'reply_trimmed'; seq: number; ts: string; round: number | null; agent: string; name: string; step: number; discardedChars: number; blocks: number }
  | { kind: 'action_repair'; seq: number; ts: string; round: number | null; agent: string; name: string; invalid: Array<{ index: number | null; tool: string; error: string }>; fixed: number }
  | { kind: 'format_retry'; seq: number; ts: string; round: number | null; agent: string; name: string; missing: string[] }
  | { kind: 'pseudo_tool_call'; seq: number; ts: string; round: number | null; agent: string; name: string; step: number; tools: string[]; discardedChars: number }
  | { kind: 'citation_check'; seq: number; ts: string; round: number | null; agent: string; name: string; cited: number; unverified: string[]; asked: boolean }
  | { kind: 'exhausted'; seq: number; ts: string; round: number | null; agent: string; name: string; steps: number; turnBytes: number }
  | { kind: 'provider_failed'; seq: number; ts: string; round: number | null; agent: string; name: string; provider: string; error: string }
  | {
      kind: 'agent_finished';
      seq: number;
      ts: string;
      round: number | null;
      agent: string;
      name: string;
      provider: string;
      durationMs: number | null;
      steps: number;
      outputBytes: number | null;
      excerpt: string;
      builtin: boolean;
    }
  | { kind: 'retrieval_warning'; seq: number; ts: string; round: number | null; agent: string; name: string; step: number; bytes: number; threshold: number; via: RetrievalVia }
  | {
      kind: 'evidence_check';
      seq: number;
      ts: string;
      round: number | null;
      agent: string;
      name: string;
      checked: number;
      mismatched: number;
      /** The items that did not match (at most 50 reach the event). */
      failures: Array<{ path: string; line: string; reason: string }>;
    }
  | {
      kind: 'compiler_index';
      seq: number;
      ts: string;
      lang: string;
      sha: string;
      status: string;
      packages: number | null;
      defs: number | null;
      refs: number | null;
      unresolved: number | null;
      ms: number | null;
      error: string;
    }
  | { kind: 'compiler_index_warning'; seq: number; ts: string; lang: string; message: string }
  | { kind: 'wiki_started'; seq: number; ts: string; pages: string[]; sha: string; team: string; language: string; force: boolean }
  | { kind: 'wiki_page_written'; seq: number; ts: string; page: string; state: string; basisFiles: number | null; citations: number | null; unverified: string[] }
  | { kind: 'wiki_page_skipped'; seq: number; ts: string; page: string; reason: string }
  | { kind: 'wiki_finished'; seq: number; ts: string; written: number; skipped: number; failed: number }
  | { kind: 'verdict'; seq: number; ts: string; approved: boolean; unverified: boolean }
  | { kind: 'jev_gate'; seq: number; ts: string; gate: string; reasons: string[] }
  | { kind: 'action'; seq: number; ts: string; id: string; tool: string; status: string }
  | { kind: 'finished'; seq: number; ts: string; status: string; consensus: boolean | null; finalRound: number | null }
  | { kind: 'error'; seq: number; ts: string; round: number | null; agent: string | null; name: string | null; message: string }
  | { kind: 'truncated'; seq: number; ts: string };

/** What a code wiki build job did, folded from the `wiki_*` events. */
export interface WikiRun {
  /** Pages the build set out to write. */
  pages: string[];
  language: string;
  force: boolean;
  written: Array<{ page: string; state: string; basisFiles: number | null; citations: number | null; unverified: string[] }>;
  skipped: Array<{ page: string; reason: string }>;
  /** From `wiki_build_finished`; null while the build runs. */
  finished: { written: number; skipped: number; failed: number } | null;
}

export interface ProposedAction {
  id: string;
  tool: string;
  status: string;
}

export interface RunState {
  /** Highest seq folded in; events at or below it are ignored. */
  lastSeq: number;
  /** `ts` of the first event. */
  startedAt: string | null;
  /** `ts` of `debate_finished` (or the last event once finalized). */
  finishedAt: string | null;
  task: string;
  project: string;
  team: string;
  sha: string;
  /** Host that ran the engine and every agent's LLM CLI (`debate_started.host`); empty for older runs. */
  host: string;
  /** Code files in the debate context (`debate_started`); null when not reported. */
  contextFiles: number | null;
  maxRounds: number | null;
  /** Latest round started (0 before the first). */
  round: number;
  /** `running` after `debate_started`, then the status of `debate_finished`; null with no start seen. */
  status: string | null;
  consensus: boolean | null;
  finalRound: number | null;
  lanes: Lane[];
  timeline: TimelineItem[];
  fallbacks: FallbackWarning[];
  totals: { steps: number; bytesRead: number; results: number };
  /** `debate_finished.retrieval`: authoritative run totals; null until then (or for old runs). */
  retrieval: { results: number; bytes: number } | null;
  verdict: { approved: boolean; unverified: boolean } | null;
  gate: { gate: string; reasons: string[] } | null;
  actions: ProposedAction[];
  /** Run-level `error` event messages (not tied to an agent). */
  errors: string[];
  /** The 8 MB event file cap was hit; later activity is not recorded. */
  truncated: boolean;
  /** `code_wiki` once `wiki_build_started` is seen; a debate otherwise. */
  kind: 'debate' | 'code_wiki';
  wiki: WikiRun | null;
}

export function initialRunState(): RunState {
  return {
    lastSeq: 0,
    startedAt: null,
    finishedAt: null,
    task: '',
    project: '',
    team: '',
    sha: '',
    host: '',
    contextFiles: null,
    maxRounds: null,
    round: 0,
    status: null,
    consensus: null,
    finalRound: null,
    lanes: [],
    timeline: [],
    fallbacks: [],
    totals: { steps: 0, bytesRead: 0, results: 0 },
    retrieval: null,
    verdict: null,
    gate: null,
    actions: [],
    errors: [],
    truncated: false,
    kind: 'debate',
    wiki: null,
  };
}

const ROLE_RANK = ['expert', 'critic', 'judge'];

function roleRank(role: string): number {
  const r = role.toLowerCase();
  const i = ROLE_RANK.findIndex((x) => r.includes(x));
  return i === -1 ? ROLE_RANK.length : i;
}

/** Per-turn warning state of a lane. */
/** Older runs carry no `via`; anything but "mcp" or "ops" counts as repo blocks. */
function viaOf(v: unknown): RetrievalVia {
  return v === 'mcp' || v === 'ops' ? v : 'block';
}

const TURN_RESET = { actionRepair: null as { invalid: number; fixed: number } | null, formatRetry: null as string[] | null, pseudoCalls: 0, viaMcp: false, deferred: 0, trimmed: 0, trimmedChars: 0, unverifiedCitations: [] as string[], citationAsked: false, noCodeRead: false };

function newLane(key: string, role: string, name: string, providers: string[]): Lane {
  return {
    key,
    role,
    name: name || key,
    providers,
    state: 'idle',
    provider: null,
    startedAt: null,
    durationMs: null,
    round: null,
    resumed: false,
    steps: 0,
    bytesRead: 0,
    resultsRead: 0,
    totalResults: 0,
    totalBytes: 0,
    outputBytes: null,
    ...TURN_RESET,
    budgetExhausted: false,
    fallbacks: [],
  };
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/** Providers of a lane not yet failed in this turn; the first is the one now answering. */
function nextProvider(lane: Lane, failed: FallbackWarning[]): string | null {
  const bad = new Set(failed.map((f) => f.provider));
  return lane.providers.find((p) => !bad.has(p)) ?? null;
}

/** Updates (or lazily creates) the lane an event belongs to. */
function withLane(lanes: Lane[], e: DebateEvent, update: (lane: Lane) => Lane): Lane[] {
  const key = e.agent;
  if (!key) return lanes;
  const i = lanes.findIndex((l) => l.key === key);
  if (i === -1) {
    const created = update(newLane(key, str(e.role), str(e.agent_name), []));
    return [...lanes, created].sort((a, b) => roleRank(a.role) - roleRank(b.role));
  }
  const next = lanes.slice();
  next[i] = update(lanes[i]);
  return next;
}

const agentName = (e: DebateEvent): string => str(e.agent_name) || str(e.agent);
const roundOf = (e: DebateEvent): number | null => num(e.round);

/** Folds one event into the state. Events at or below `lastSeq` (replays after a reconnect) are ignored. */
export function reduceEvent(state: RunState, e: DebateEvent): RunState {
  if (e.seq <= state.lastSeq) return state;
  const base: RunState = { ...state, lastSeq: e.seq, startedAt: state.startedAt ?? e.ts };
  const add = (item: TimelineItem): TimelineItem[] => [...base.timeline, item];

  switch (e.type) {
    case 'debate_started': {
      const d = e.data ?? {};
      const members = Array.isArray(d.members) ? d.members : [];
      const lanes = members
        .map((m) => newLane(str(m.agent), str(m.role), str(m.agent_name), Array.isArray(m.providers) ? m.providers.map(String) : []))
        .filter((l) => l.key !== '')
        .sort((a, b) => roleRank(a.role) - roleRank(b.role));
      const sha = str(d.context?.sha);
      return {
        ...base,
        task: str(d.task),
        project: str(d.project),
        team: str(d.team),
        sha,
        host: str(d.host),
        contextFiles: num(d.context?.files),
        maxRounds: num(d.max_rounds),
        status: 'running',
        lanes,
        timeline: add({ kind: 'run_started', seq: e.seq, ts: e.ts, members, sha, files: num(d.context?.files), maxRounds: num(d.max_rounds) }),
      };
    }
    case 'round_started': {
      const round = roundOf(e) ?? base.round + 1;
      return {
        ...base,
        round,
        // A new round starts every lane afresh; what a lane did before stays in the timeline.
        lanes: base.lanes.map((l) =>
          l.state === 'running' || l.state === 'retrieving'
            ? l
            : { ...l, state: 'idle', startedAt: null, steps: 0, bytesRead: 0, resultsRead: 0, budgetExhausted: false, fallbacks: [], ...TURN_RESET }
        ),
        timeline: add({ kind: 'round', seq: e.seq, ts: e.ts, round }),
      };
    }
    case 'agent_started': {
      const d = e.data ?? {};
      const round = roundOf(e);
      return {
        ...base,
        round: round !== null ? Math.max(base.round, round) : base.round,
        lanes: withLane(base.lanes, e, (l) => ({
          ...l,
          state: 'running',
          startedAt: e.ts,
          durationMs: null,
          round,
          resumed: d.resumed === true,
          steps: 0,
          bytesRead: 0,
          resultsRead: 0,
          outputBytes: null,
          budgetExhausted: false,
          fallbacks: [],
          ...TURN_RESET,
          provider: l.providers[0] ?? l.provider,
        })),
        timeline: add({
          kind: 'agent_started', seq: e.seq, ts: e.ts, round, agent: str(e.agent), name: agentName(e), role: str(e.role),
          resumed: d.resumed === true, promptBytes: num(d.prompt_bytes),
          promptSaved: d.prompt_saved === true, promptTruncated: d.prompt_truncated === true,
        }),
      };
    }
    case 'provider_failed': {
      const d = e.data ?? {};
      const warning: FallbackWarning = {
        seq: e.seq, round: roundOf(e), agent: str(e.agent), agentName: agentName(e), provider: str(d.provider), error: str(d.error),
      };
      return {
        ...base,
        fallbacks: [...base.fallbacks, warning],
        lanes: withLane(base.lanes, e, (l) => {
          const fallbacks = [...l.fallbacks, warning];
          return { ...l, fallbacks, provider: nextProvider(l, fallbacks) ?? l.provider };
        }),
        timeline: add({ kind: 'provider_failed', seq: e.seq, ts: e.ts, round: warning.round, agent: warning.agent, name: warning.agentName, provider: warning.provider, error: warning.error }),
      };
    }
    case 'retrieval_requested': {
      const d = e.data ?? {};
      const step = num(d.step) ?? 0;
      const via = viaOf(d.via);
      const requests = Array.isArray(d.requests) ? d.requests.map((r) => ({ op: str(r.op), target: str(r.target) })) : [];
      return {
        ...base,
        totals: { ...base.totals, steps: base.totals.steps + 1 },
        lanes: withLane(base.lanes, e, (l) => ({ ...l, state: 'retrieving', steps: Math.max(l.steps, step), viaMcp: l.viaMcp || via === 'mcp' })),
        timeline: add({
          kind: 'retrieval', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), step, requests, results: null, turnBytes: null, via,
        }),
      };
    }
    case 'retrieval_served': {
      const d = e.data ?? {};
      const step = num(d.step) ?? 0;
      const results = Array.isArray(d.results)
        ? d.results.map((r) => ({
            op: str(r.op), target: str(r.target), ok: r.ok === true, bytes: num(r.bytes) ?? 0,
            ...(r.deferred === true || r.error === 'deferred' ? { deferred: true } : {}),
            ...(r.truncated ? { truncated: true } : {}), ...(r.error ? { error: str(r.error) } : {}),
          }))
        : [];
      const served = results.reduce((n, r) => n + (r.ok ? r.bytes : 0), 0);
      const servedCount = results.filter((r) => r.ok).length;
      const deferred = results.filter((r) => r.deferred).length;
      const turnBytes = num(d.turn_bytes);
      const via = viaOf(d.via);
      const agent = str(e.agent);
      // Attach to the request of the same step that is still waiting for its answer.
      let at = -1;
      for (let i = base.timeline.length - 1; i >= 0; i--) {
        const t = base.timeline[i];
        if (t.kind === 'retrieval' && t.agent === agent && t.step === step && t.results === null) {
          at = i;
          break;
        }
      }
      let timeline: TimelineItem[];
      if (at === -1) {
        timeline = add({
          kind: 'retrieval', seq: e.seq, ts: e.ts, round: roundOf(e), agent, name: agentName(e), step,
          requests: results.map((r) => ({ op: r.op, target: r.target })), results, turnBytes, via,
        });
      } else {
        timeline = base.timeline.slice();
        timeline[at] = {
          ...(base.timeline[at] as Extract<TimelineItem, { kind: 'retrieval' }>),
          results,
          turnBytes,
          // The served event is authoritative when it names a route; otherwise keep the request's.
          via: d.via === undefined ? (base.timeline[at] as Extract<TimelineItem, { kind: 'retrieval' }>).via : via,
        };
      }
      return {
        ...base,
        timeline,
        totals: { ...base.totals, bytesRead: base.totals.bytesRead + served, results: base.totals.results + servedCount },
        lanes: withLane(base.lanes, e, (l) => ({
          ...l,
          state: l.state === 'retrieving' ? 'running' : l.state,
          steps: Math.max(l.steps, step),
          viaMcp: l.viaMcp || via === 'mcp',
          deferred: l.deferred + deferred,
          bytesRead: turnBytes ?? l.bytesRead + served,
          resultsRead: l.resultsRead + servedCount,
          totalResults: l.totalResults + servedCount,
          totalBytes: l.totalBytes + served,
        })),
      };
    }
    case 'reply_trimmed': {
      const d = e.data ?? {};
      const discardedChars = num(d.discarded_chars) ?? 0;
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({ ...l, trimmed: l.trimmed + 1, trimmedChars: l.trimmedChars + discardedChars })),
        timeline: add({
          kind: 'reply_trimmed', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e),
          step: num(d.step) ?? 0, discardedChars, blocks: num(d.blocks) ?? 1,
        }),
      };
    }
    case 'action_repair': {
      const d = e.data ?? {};
      const invalid = Array.isArray(d.invalid)
        ? d.invalid.map((x) => ({ index: num(x?.index) ?? null, tool: str(x?.tool), error: str(x?.error) }))
        : [];
      const fixed = Math.min(num(d.fixed) ?? 0, invalid.length);
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({ ...l, actionRepair: { invalid: invalid.length, fixed } })),
        timeline: add({ kind: 'action_repair', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), invalid, fixed }),
      };
    }
    case 'format_retry': {
      const d = e.data ?? {};
      const missing = Array.isArray(d.missing) ? d.missing.map(String) : [];
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({ ...l, formatRetry: missing })),
        timeline: add({ kind: 'format_retry', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), missing }),
      };
    }
    case 'pseudo_tool_call': {
      const d = e.data ?? {};
      const tools = Array.isArray(d.tools) ? d.tools.map(String) : [];
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({ ...l, pseudoCalls: l.pseudoCalls + 1 })),
        timeline: add({
          kind: 'pseudo_tool_call', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e),
          step: num(d.step) ?? 0, tools, discardedChars: num(d.discarded_chars) ?? 0,
        }),
      };
    }
    case 'citation_check': {
      const d = e.data ?? {};
      const unverified = Array.isArray(d.unverified) ? d.unverified.map(String) : [];
      const asked = d.asked === true;
      return {
        ...base,
        // The latest check of a turn wins: a clean re-check clears an earlier warning.
        lanes: withLane(base.lanes, e, (l) => ({ ...l, unverifiedCitations: unverified, citationAsked: asked })),
        timeline: add({
          kind: 'citation_check', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e),
          cited: num(d.cited) ?? 0, unverified, asked,
        }),
      };
    }
    case 'retrieval_exhausted': {
      const d = e.data ?? {};
      const steps = num(d.steps) ?? 0;
      const turnBytes = num(d.turn_bytes) ?? 0;
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({ ...l, budgetExhausted: true, state: l.state === 'retrieving' ? 'running' : l.state })),
        timeline: add({ kind: 'exhausted', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), steps, turnBytes }),
      };
    }
    case 'agent_finished': {
      const d = e.data ?? {};
      const provider = str(d.provider);
      const builtin = d.fallback === true || provider === 'builtin';
      const steps = num(d.steps);
      return {
        ...base,
        lanes: withLane(base.lanes, e, (l) => ({
          ...l,
          state: builtin ? 'builtin' : 'done',
          provider: provider || l.provider,
          startedAt: null,
          durationMs: num(d.duration_ms),
          steps: steps ?? l.steps,
          outputBytes: num(d.output_bytes),
          noCodeRead: !builtin && (steps ?? l.steps) === 0 && base.contextFiles === 0,
        })),
        timeline: add({
          kind: 'agent_finished', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), provider,
          durationMs: num(d.duration_ms), steps: steps ?? 0, outputBytes: num(d.output_bytes), excerpt: str(d.excerpt), builtin,
        }),
      };
    }
    case 'retrieval_warning': {
      const d = e.data ?? {};
      return {
        ...base,
        timeline: add({
          kind: 'retrieval_warning', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e),
          step: num(d.step) ?? 0, bytes: num(d.bytes) ?? 0, threshold: num(d.threshold) ?? 0, via: viaOf(d.via),
        }),
      };
    }
    case 'evidence_check': {
      const d = e.data ?? {};
      const failures = (Array.isArray(d.items) ? d.items : [])
        .filter((x) => x && x.ok !== true)
        .map((x) => ({ path: str(x.path), line: x.line === undefined || x.line === null ? '' : String(x.line), reason: str(x.reason) }));
      const checked = num(d.checked) ?? 0;
      const mismatched = num(d.mismatched) ?? failures.length;
      return {
        ...base,
        timeline: add({ kind: 'evidence_check', seq: e.seq, ts: e.ts, round: roundOf(e), agent: str(e.agent), name: agentName(e), checked, mismatched, failures }),
      };
    }
    case 'compiler_index': {
      const d = e.data ?? {};
      return {
        ...base,
        timeline: add({
          kind: 'compiler_index', seq: e.seq, ts: e.ts, lang: str(d.lang) || 'go', sha: str(d.sha), status: str(d.status) || 'unknown',
          packages: num(d.packages), defs: num(d.defs), refs: num(d.refs), unresolved: num(d.unresolved), ms: num(d.ms), error: str(d.error),
        }),
      };
    }
    case 'compiler_index_warning': {
      const d = e.data ?? {};
      return { ...base, timeline: add({ kind: 'compiler_index_warning', seq: e.seq, ts: e.ts, lang: str(d.lang) || 'go', message: str(d.message) }) };
    }
    case 'wiki_build_started': {
      const d = e.data ?? {};
      const pages = Array.isArray(d.pages) ? d.pages.map(String) : [];
      const sha = str(d.sha);
      return {
        ...base,
        kind: 'code_wiki',
        task: tr.wiki.jobLabel,
        project: str(d.project),
        team: str(d.team),
        sha,
        host: str(d.host),
        status: 'running',
        wiki: { pages, language: str(d.language), force: d.force === true, written: [], skipped: [], finished: null },
        timeline: add({ kind: 'wiki_started', seq: e.seq, ts: e.ts, pages, sha, team: str(d.team), language: str(d.language), force: d.force === true }),
      };
    }
    case 'wiki_page_written': {
      const d = e.data ?? {};
      const entry = {
        page: str(d.page), state: str(d.state), basisFiles: num(d.basis_files), citations: num(d.citations),
        unverified: Array.isArray(d.unverified) ? d.unverified.map(String) : [],
      };
      const wiki = base.wiki ?? { pages: [], language: '', force: false, written: [], skipped: [], finished: null };
      return {
        ...base,
        wiki: { ...wiki, written: [...wiki.written, entry] },
        timeline: add({ kind: 'wiki_page_written', seq: e.seq, ts: e.ts, ...entry }),
      };
    }
    case 'wiki_page_skipped': {
      const d = e.data ?? {};
      const entry = { page: str(d.page), reason: str(d.reason) };
      const wiki = base.wiki ?? { pages: [], language: '', force: false, written: [], skipped: [], finished: null };
      return {
        ...base,
        wiki: { ...wiki, skipped: [...wiki.skipped, entry] },
        timeline: add({ kind: 'wiki_page_skipped', seq: e.seq, ts: e.ts, ...entry }),
      };
    }
    case 'wiki_build_finished': {
      const d = e.data ?? {};
      const finished = { written: num(d.written) ?? 0, skipped: num(d.skipped) ?? 0, failed: num(d.failed) ?? 0 };
      const wiki = base.wiki ?? { pages: [], language: '', force: false, written: [], skipped: [], finished: null };
      return {
        ...base,
        // A build that wrote nothing and failed somewhere is a failed job; otherwise it completed (skips are reported, not errors).
        status: finished.failed > 0 && finished.written === 0 ? 'failed' : 'completed',
        finishedAt: e.ts,
        wiki: { ...wiki, finished },
        lanes: base.lanes.map(settleLane),
        timeline: add({ kind: 'wiki_finished', seq: e.seq, ts: e.ts, ...finished }),
      };
    }
    case 'verdict': {
      const d = e.data ?? {};
      const verdict = { approved: d.approved === true, unverified: d.unverified === true };
      return { ...base, verdict, timeline: add({ kind: 'verdict', seq: e.seq, ts: e.ts, ...verdict }) };
    }
    case 'jev_gate': {
      const d = e.data ?? {};
      const gate = { gate: str(d.gate), reasons: Array.isArray(d.reasons) ? d.reasons.map(String) : [] };
      return { ...base, gate, timeline: add({ kind: 'jev_gate', seq: e.seq, ts: e.ts, ...gate }) };
    }
    case 'action_proposed': {
      const d = e.data ?? {};
      const action = { id: d.id === undefined ? '' : String(d.id), tool: str(d.tool), status: str(d.status) };
      return { ...base, actions: [...base.actions, action], timeline: add({ kind: 'action', seq: e.seq, ts: e.ts, ...action }) };
    }
    case 'debate_finished': {
      const d = e.data ?? {};
      const status = str(d.status) || 'finished';
      const r = d.retrieval;
      const retrieval = r && typeof r === 'object' ? { results: num(r.results) ?? 0, bytes: num(r.bytes) ?? 0 } : null;
      const byAgent: Record<string, { results?: unknown; bytes?: unknown }> =
        r && typeof r === 'object' && r.by_agent && typeof r.by_agent === 'object' ? r.by_agent : {};
      return {
        ...base,
        status,
        retrieval,
        finishedAt: e.ts,
        consensus: typeof d.consensus === 'boolean' ? d.consensus : null,
        finalRound: num(d.final_round),
        // Nothing runs after the end: a lane still marked in flight did not complete.
        lanes: base.lanes.map(settleLane).map((l) => {
          const a = byAgent[l.key];
          return a ? { ...l, totalResults: num(a.results) ?? l.totalResults, totalBytes: num(a.bytes) ?? l.totalBytes } : l;
        }),
        timeline: add({ kind: 'finished', seq: e.seq, ts: e.ts, status, consensus: typeof d.consensus === 'boolean' ? d.consensus : null, finalRound: num(d.final_round) }),
      };
    }
    case 'error': {
      const message = str(e.data?.message);
      const agent = e.agent ? str(e.agent) : null;
      return {
        ...base,
        errors: agent ? base.errors : [...base.errors, message],
        lanes: agent ? withLane(base.lanes, e, (l) => ({ ...l, state: 'failed', startedAt: null })) : base.lanes,
        timeline: add({ kind: 'error', seq: e.seq, ts: e.ts, round: roundOf(e), agent, name: agent ? agentName(e) : null, message }),
      };
    }
    case 'events_truncated':
      return { ...base, truncated: true, timeline: add({ kind: 'truncated', seq: e.seq, ts: e.ts }) };
    default:
      // A type this version does not know: keep the cursor moving, show nothing.
      return base;
  }
}

function settleLane(l: Lane): Lane {
  return l.state === 'running' || l.state === 'retrieving' ? { ...l, state: 'failed', startedAt: null } : l;
}

/** Folds many events in order. */
export function reduceEvents(state: RunState, events: DebateEvent[]): RunState {
  return events.reduce(reduceEvent, state);
}

/**
 * For a job that is no longer running but never wrote `debate_finished` (cancelled, interrupted,
 * crashed): lanes still in flight are failed and the status is taken from the job.
 */
export function finalizeRun(state: RunState, jobStatus: string): RunState {
  if (state.status !== null && state.status !== 'running') return state;
  const last = state.timeline[state.timeline.length - 1];
  return { ...state, status: jobStatus, finishedAt: state.finishedAt ?? last?.ts ?? null, lanes: state.lanes.map(settleLane) };
}

// --- Presentation helpers ----------------------------------------------------------------------

export function laneTone(state: LaneState): Tone {
  switch (state) {
    case 'running':
    case 'retrieving':
      return 'info';
    case 'done':
      return 'ok';
    case 'failed':
      return 'crit';
    case 'builtin':
      return 'warn';
    default:
      return 'idle';
  }
}

export function laneLabel(state: LaneState): string {
  return tr.agents.laneState[state] ?? state;
}

/** Korean label of a jev gate result; the raw value stays the chip title. */
export function gateLabel(gate: string | undefined): string {
  return gate ? (tr.agents.gate[gate.toUpperCase()] ?? gate.toUpperCase()) : tr.agents.gate.UNKNOWN;
}

/** Korean label of an action status; the raw value stays the chip title. */
export function actionLabel(status: string | undefined): string {
  const key = (status || 'unknown').toUpperCase();
  return tr.agents.actionStatus[key] ?? key;
}

/** Run-level retrieval cost: the finished event's totals win over the live sum. */
export function runRetrieval(run: RunState): { results: number; bytes: number } | null {
  if (run.retrieval) return run.retrieval;
  return run.totals.results > 0 || run.totals.bytesRead > 0 ? { results: run.totals.results, bytes: run.totals.bytesRead } : null;
}

export function laneActive(lane: Lane): boolean {
  return lane.state === 'running' || lane.state === 'retrieving';
}

function parseTs(ts: string | null): number | null {
  if (!ts) return null;
  const t = Date.parse(ts);
  return Number.isNaN(t) ? null : t;
}

/** Milliseconds the lane's turn has taken: live while in flight, the recorded duration once finished, else null. */
export function laneElapsedMs(lane: Lane, now: number): number | null {
  if (laneActive(lane)) {
    const start = parseTs(lane.startedAt);
    return start === null ? null : Math.max(0, now - start);
  }
  return lane.durationMs;
}

/** Milliseconds since the run began (to its end when finished). */
export function runElapsedMs(run: RunState, now: number): number | null {
  const start = parseTs(run.startedAt);
  if (start === null) return null;
  const end = run.status !== null && run.status !== 'running' ? parseTs(run.finishedAt) : null;
  return Math.max(0, (end ?? now) - start);
}

/** `read apps/agent/main.go:100-180` */
export function requestLabel(r: { op: string; target: string }): string {
  return [r.op, r.target].filter(Boolean).join(' ');
}

/** "+1분 03초" offset of an event from the start of the run. */
export function offsetLabel(ts: string, startedAt: string | null): string {
  const t = parseTs(ts);
  const s = parseTs(startedAt);
  if (t === null || s === null) return '';
  const sec = Math.max(0, Math.round((t - s) / 1000));
  const m = Math.floor(sec / 60);
  return m ? tr.agents.offset(m, sec % 60) : tr.agents.offsetSeconds(sec);
}

/** Running jobs first, then the rest; each group newest first. Does not mutate. */
export function sortRuns<T extends { status: string; started_at: string; id: string }>(jobs: T[]): T[] {
  const rank = (j: T) => (j.status === 'running' ? 0 : 1);
  return jobs
    .slice()
    .sort((a, b) => rank(a) - rank(b) || (parseTs(b.started_at) ?? 0) - (parseTs(a.started_at) ?? 0) || (a.id < b.id ? 1 : -1));
}

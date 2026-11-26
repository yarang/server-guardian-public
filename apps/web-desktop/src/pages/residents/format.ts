import type { Tone } from '@server-guardian/ui';
import { fmtDuration, parseTime } from '../../lib/debate';
import { tr } from '../../i18n';

/** Small pure helpers of the resident monitor tab (kept here for unit tests). */

/** Tone of an agent state chip; an unknown state reads neutral. */
export function agentStateTone(state: string | null | undefined): Tone {
  switch (state) {
    case 'ready':
      return 'ok';
    case 'busy':
    case 'binding':
      return 'info';
    case 'retiring':
    case 'bind_failed':
      return 'warn';
    case 'down':
      return 'crit';
    default:
      return 'idle';
  }
}

/** Label of an agent state through the catalog; an unknown value stays as it is. */
export function agentStateLabel(state: string | null | undefined): string {
  return state ? (tr.residents.agentState[state] ?? state) : tr.common.unknown;
}

/** Tone of a mail state chip; an unknown state reads neutral. */
export function mailStateTone(state: string | null | undefined): Tone {
  switch (state) {
    case 'retrieved':
      return 'ok';
    case 'queued':
    case 'delivered':
      return 'info';
    case 'expired':
      return 'crit';
    default:
      return 'idle';
  }
}

/** Label of a mail state through the catalog; an unknown value stays as it is. */
export function mailStateLabel(state: string | null | undefined): string {
  return state ? (tr.residents.mailState[state] ?? state) : tr.common.unknown;
}

/** Label of a task status (the board's columns) through the catalog; unknown stays as it is. */
export function taskStatusLabel(status: string | null | undefined): string {
  return status ? (tr.residents.taskStatus[status] ?? status) : tr.common.unknown;
}

/** Elapsed time of something still running, counted from `started` to `now`; '—' when unknown. */
export function elapsedSince(started: string | null | undefined, now: number): string {
  const t = parseTime(started);
  return t === null ? tr.common.dash : fmtDuration(now - t);
}

/** Length of a finished turn (`ended` - `started`); '—' when either time is unknown. */
export function turnDuration(started: string | null | undefined, ended: string | null | undefined): string {
  const a = parseTime(started);
  const b = parseTime(ended);
  return a !== null && b !== null && b >= a ? fmtDuration(b - a) : tr.common.dash;
}

/** First 7 hex chars of a commit sha (the short form git shows); '' when there is none. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : '';
}

/** An event's `detail` object as compact `key=value` pairs, in stored order; '' when empty. */
export function detailPairs(detail: Record<string, unknown> | null | undefined): string {
  if (!detail) return '';
  return Object.entries(detail)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' ');
}

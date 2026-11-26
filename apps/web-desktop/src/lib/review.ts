import { isCharacter } from './personas';
import type { PersonaConfig, ReviewFinding, ReviewResult, ReviewSeverity } from '../types';

/** Quick review (docs/design/verification-layer.md, V1c): route params, form checks and the "Run as debate" task. */

export interface ReviewRoute {
  id?: string;
}

/** The id form the hub gives a review (`review_20261123_101112_ab12cd`). */
const REVIEW_ID = /^review_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$/;
/** BASE..HEAD of two refs, the shape the hub accepts (no `...`, no leading dash). */
const RANGE = /^[A-Za-z0-9_/~^][A-Za-z0-9._/~^-]{0,99}\.\.[A-Za-z0-9_/~^][A-Za-z0-9._/~^-]{0,99}$/;
/** The hub's patch limit (2 MiB, as the CLI's). */
export const REVIEW_PATCH_MAX = 2 * 1024 * 1024;
/** A running review is polled this often; a finished one is fetched once. */
export const REVIEW_POLL_MS = 2000;
export const REVIEW_LIST_POLL_MS = 10000;
export const SEVERITIES: readonly ReviewSeverity[] = ['high', 'medium', 'low'];

export function parseReviewRoute(params: string[]): ReviewRoute {
  const [id] = params;
  return id && REVIEW_ID.test(id) ? { id } : {};
}

export function reviewParams(r: ReviewRoute): string[] {
  return r.id ? ['review', r.id] : ['review'];
}

export function validRange(text: string): boolean {
  return RANGE.test(text.trim()) && !text.includes('...');
}

/** UTF-8 size of a patch, to refuse one above the hub's limit before sending it. */
export function patchBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * Personas that may review: a character needs the critic or solo seat (the runner checks the same); a format 1
 * persona keeps the id rule (a role that judges or proposes actions is refused).
 */
export function reviewRoles(personas: PersonaConfig[]): PersonaConfig[] {
  return personas.filter((p) =>
    isCharacter(p)
      ? (p.seats ?? []).some((s) => s === 'critic' || s === 'solo')
      : !p.id.includes('judge') && !p.id.includes('jev_mcp'),
  );
}

export function severityTone(severity: ReviewSeverity): 'crit' | 'warn' | 'idle' {
  return severity === 'high' ? 'crit' : severity === 'medium' ? 'warn' : 'idle';
}

export function findingsBySeverity(findings: ReviewFinding[]): Record<ReviewSeverity, ReviewFinding[]> {
  const out: Record<ReviewSeverity, ReviewFinding[]> = { high: [], medium: [], low: [] };
  for (const f of findings) (out[f.severity] ?? out.low).push(f);
  return out;
}

/**
 * The task of "Run as debate": review the same change, with the quick review's findings as unverified background.
 * The debate form refuses a task above `max` characters, so the findings list is cut to fit; the head stays whole.
 */
export function debateTaskFrom(result: ReviewResult, intro: string, heading: string, max: number): string {
  const what = result.change.range ?? `${result.base.slice(0, 12)}..${result.head.slice(0, 12)}`;
  const head = `${intro} ${what} (${result.files.join(', ')})\n\n${heading}\n`;
  const lines = result.findings.map((f) => `- [${f.severity}] ${f.path}:${f.line} ${f.claim}`).join('\n');
  const room = max - head.length;
  if (room <= 1) return head.slice(0, max);
  return head + (lines.length > room ? `${lines.slice(0, room - 1)}…` : lines);
}

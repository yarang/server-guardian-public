import { isCharacter } from './personas';
import type { PersonaConfig } from '../types';

/** Quick ask (solo agent, docs/design/solo-agent.md S3): route params, the answer's machine tail, form helpers. */

/** Route params of the Quick ask tab: `#/debates/ask[/<id>]`. */
export interface AskRoute {
  id?: string;
}

/** The id form the hub gives a solo run (`solo_20261030_101112_ab12cd`). */
const SOLO_ID = /^solo_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$/;

export function parseAskRoute(params: string[]): AskRoute {
  const [id] = params;
  return id && SOLO_ID.test(id) ? { id } : {};
}

export function askParams(r: AskRoute): string[] {
  return r.id ? ['ask', r.id] : ['ask'];
}

/** A running solo run is polled this often; a finished one is fetched once (and by the global Refresh). */
export const SOLO_POLL_MS = 2000;
/** The recent-runs list refreshes this often while the tab is open. */
export const SOLO_LIST_POLL_MS = 10000;

/** Fallback when the hub does not report its providers (an older hub, or the teams request failed). */
export const SOLO_PROVIDERS = ['claude', 'codex', 'agy'] as const;

/** Providers a general question may not use: mirrors `GENERAL_EXCLUDED` in solo_agent.py (enabled for code work
 *  only). exaone is allowed. */
export const GENERAL_EXCLUDED_PROVIDERS = ['zai'] as const;

/**
 * Providers the form offers: every one the hub has installed (`catalog.installed` of the teams endpoint), or the
 * fixed fallback when it reports none. A general question drops the ones the hub refuses for it. The empty choice
 * (first available, with fallback) is added by the form.
 */
export function soloProviders(installed: readonly string[] | undefined, general: boolean): string[] {
  const names = installed && installed.length > 0 ? [...installed] : [...SOLO_PROVIDERS];
  return general ? names.filter((n) => !(GENERAL_EXCLUDED_PROVIDERS as readonly string[]).includes(n)) : names;
}

/** True when a run or list row is a general question (no project); an absent mode is an older hub or a run without one. */
export function isGeneral(x: { mode?: string } | undefined): boolean {
  return x?.mode === 'general';
}

/** Removes a trailing ```findings fenced block (machine-readable tail); the prose before it stays. */
export function stripFindings(answer: string): string {
  const stripped = answer.replace(/(?:^|\n)```findings[^\n]*\n[\s\S]*?\n?```\s*$/, '');
  return stripped === answer ? answer : stripped.trimEnd();
}

/** One path per line, trimmed, empties dropped. */
export function splitPaths(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l !== '');
}

/**
 * Personas a solo run may answer as. A character (format 2) is offered only when it declares the `solo` seat
 * (the runner reads `seats` too); a format 1 persona keeps the id rule (a role that only judges cannot answer,
 * the CLI refuses the same ids).
 */
export function soloRoles(personas: PersonaConfig[]): PersonaConfig[] {
  return personas.filter((p) =>
    isCharacter(p) ? (p.seats ?? []).includes('solo') : !p.id.includes('judge') && !p.id.includes('jev_mcp'),
  );
}

/**
 * The task of "Run as debate": the question, then the solo answer as unverified background. The debate form
 * refuses a task above `max` characters, so a long answer is cut (with an ellipsis) to fit; the question stays whole.
 */
export function debateTaskFrom(question: string, answer: string, heading: string, max: number): string {
  const head = `${question}\n\n${heading}\n`;
  const room = max - head.length;
  if (answer.length <= room) return head + answer;
  return room > 1 ? `${head}${answer.slice(0, room - 1).trimEnd()}…` : question.slice(0, max);
}

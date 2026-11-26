import { describe, expect, it } from 'vitest';
import type { CharacterSeat, PersonaConfig } from '../src/types';
import {
  SOLO_PROVIDERS,
  soloProviders,
  askParams,
  debateTaskFrom,
  isGeneral,
  parseAskRoute,
  soloRoles,
  splitPaths,
  stripFindings,
} from '../src/lib/solo';

const persona = (id: string): PersonaConfig => ({ id, name: id, role: 'r', domain: 'd', system_prompt: 'p' });

/** A character (format 2): the seats decide where it may run (agent-characters.md C2). */
const character = (seats: CharacterSeat[]): PersonaConfig => ({
  id: 'safety_reviewer',
  name: 'Safety reviewer',
  format: 2,
  seats,
  persona: { identity: 'a reviewer', expertise: ['data loss'], stance: 'Cleanup is the goal.', checks: ['what it deletes'] },
});

describe('ask route', () => {
  it('accepts nothing or a solo run id', () => {
    const id = 'solo_20261030_101112_ab12cd';
    expect(parseAskRoute([])).toEqual({});
    expect(parseAskRoute([id])).toEqual({ id });
    // Anything else (a wrong id form, a stray segment) is the home view, not a broken detail.
    expect(parseAskRoute(['ask'])).toEqual({});
    expect(parseAskRoute(['solo_20261030_101112_AB12CD'])).toEqual({});
    expect(parseAskRoute(['solo_1'])).toEqual({});
  });

  it('builds the params of the ask route', () => {
    expect(askParams({})).toEqual(['ask']);
    expect(askParams({ id: 'solo_1' })).toEqual(['ask', 'solo_1']);
  });
});

describe('stripFindings', () => {
  it('removes a trailing findings block and keeps the prose', () => {
    const answer = 'The retry loop lives in `runner.go:40`.\n\n```findings\n[{"id": 1}]\n```\n';
    expect(stripFindings(answer)).toBe('The retry loop lives in `runner.go:40`.');
  });

  it('removes an empty trailing block too', () => {
    expect(stripFindings('Answer.\n\n```findings\n```')).toBe('Answer.');
  });

  it('leaves an answer without one unchanged, including its trailing whitespace', () => {
    expect(stripFindings('Plain answer.')).toBe('Plain answer.');
    expect(stripFindings('Trailing newline.\n')).toBe('Trailing newline.\n');
  });

  it('keeps a findings block that is not the tail', () => {
    const answer = '```findings\n[]\n```\nAfter the block.';
    expect(stripFindings(answer)).toBe(answer);
  });
});

describe('splitPaths', () => {
  it('takes one path per line, trimmed, empties dropped', () => {
    expect(splitPaths('  apps/agent/*.go \n\ncli/**/*.py\n')).toEqual(['apps/agent/*.go', 'cli/**/*.py']);
    expect(splitPaths('')).toEqual([]);
  });
});

describe('soloRoles', () => {
  it('drops the personas a solo run refuses: judges', () => {
    const personas = [persona('sec-expert'), persona('ops_judge'), persona('jev_mcp_decision'), persona('design-critic')];
    expect(soloRoles(personas).map((p) => p.id)).toEqual(['sec-expert', 'design-critic']);
  });

  it('offers a character only when it declares the solo seat', () => {
    const personas = [persona('sec-expert'), character(['critic', 'verifier']), character(['expert', 'solo'])];
    expect(soloRoles(personas).map((p) => p.id)).toEqual(['sec-expert', 'safety_reviewer']);
  });

  it('drops a character with no seats and one that only judges', () => {
    expect(soloRoles([character([]), character(['judge'])]).map((p) => p.id)).toEqual([]);
  });
});

describe('SOLO_PROVIDERS', () => {
  it('names the providers the CLI knows (ids without the -cli suffix it reports back)', () => {
    expect(SOLO_PROVIDERS).toEqual(['claude', 'codex', 'agy']);
  });
});

describe('soloProviders', () => {
  it('offers every provider the hub reports installed', () => {
    expect(soloProviders(['claude', 'zai', 'opencode', 'ollama'], false)).toEqual(['claude', 'zai', 'opencode', 'ollama']);
  });

  it('drops zai for a general question, which the hub refuses', () => {
    expect(soloProviders(['claude', 'zai', 'opencode'], true)).toEqual(['claude', 'opencode']);
  });

  it('falls back to the fixed list when the hub reports none', () => {
    expect(soloProviders(undefined, false)).toEqual([...SOLO_PROVIDERS]);
    expect(soloProviders([], true)).toEqual([...SOLO_PROVIDERS]);
  });
});

describe('isGeneral', () => {
  it('is true only for a general run or row', () => {
    expect(isGeneral({ mode: 'general' })).toBe(true);
    expect(isGeneral({ mode: 'code' })).toBe(false);
    expect(isGeneral({ mode: 'ops' })).toBe(false);
    // An older hub, and running/failed list rows, carry no mode: that is never general.
    expect(isGeneral({})).toBe(false);
    expect(isGeneral(undefined)).toBe(false);
  });
});

describe('debateTaskFrom', () => {
  it('keeps a short answer whole', () => {
    expect(debateTaskFrom('Q?', 'A.', 'Earlier:', 100)).toBe('Q?\n\nEarlier:\nA.');
  });
  it('cuts a long answer to the limit with an ellipsis and keeps the question', () => {
    const out = debateTaskFrom('Q?', 'x'.repeat(500), 'Earlier:', 100);
    expect(out.length).toBeLessThanOrEqual(100);
    expect(out.startsWith('Q?\n\nEarlier:\n')).toBe(true);
    expect(out.endsWith('…')).toBe(true);
  });
});

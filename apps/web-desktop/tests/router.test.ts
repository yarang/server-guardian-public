import { describe, expect, it } from 'vitest';
import { ROUTES, fleetHref, fleetTab, hrefFor, parseHash } from '../src/router';

describe('parseHash', () => {
  it('parses route, params and query', () => {
    expect(parseHash('#/fleet/nodes/a1?x=1&y=two')).toEqual({ route: 'fleet', params: ['nodes', 'a1'], query: { x: '1', y: 'two' } });
    expect(parseHash('#debates/job%2F7/extra')).toEqual({ route: 'debates', params: ['job/7', 'extra'], query: {} });
    expect(parseHash('#/fleet/commands?target=a1')).toEqual({ route: 'fleet', params: ['commands'], query: { target: 'a1' } });
    expect(parseHash('#/approvals/9')).toEqual({ route: 'approvals', params: ['9'], query: {} });
    // Node groups moved from Settings to the Fleet Groups tab; the old deep link keeps working.
    expect(parseHash('#/settings/groups?edit=lab')).toEqual({ route: 'fleet', params: ['groups'], query: { edit: 'lab' } });
    expect(parseHash('#/fleet/groups')).toEqual({ route: 'fleet', params: ['groups'], query: {} });
    expect(parseHash('#/settings/teams?edit=x')).toEqual({ route: 'settings', params: ['teams'], query: { edit: 'x' } });
  });

  it('defaults to the Fleet summary for empty and unknown hashes', () => {
    expect(parseHash('')).toEqual({ route: 'fleet', params: [], query: {} });
    expect(parseHash('#/')).toMatchObject({ route: 'fleet' });
    expect(parseHash('#/nope/x?y=1')).toEqual({ route: 'fleet', params: [], query: {} });
  });

  it('has exactly four destinations', () => {
    expect(ROUTES.map((r) => r.id)).toEqual(['fleet', 'debates', 'approvals', 'settings']);
  });

  it.each([
    ['#/overview', 'fleet', []],
    ['#/assistant', 'fleet', []],
    ['#/more', 'fleet', []],
    ['#/nodes', 'fleet', ['nodes']],
    ['#/nodes/a1', 'fleet', ['nodes', 'a1']],
    ['#/nodes/new', 'fleet', ['nodes', 'new']],
    ['#/security', 'fleet', ['ssh']],
    ['#/ssh', 'fleet', ['ssh']],
    ['#/run', 'fleet', ['commands']],
    ['#/batch', 'fleet', ['commands']],
    ['#/terminal', 'fleet', ['commands']],
    ['#/agents', 'debates', []],
    ['#/agents/j1', 'debates', ['j1']],
    ['#/debate', 'debates', []],
    ['#/debate/j1', 'debates', ['j1']],
  ])('resolves the legacy hash %s', (hash, route, params) => {
    expect(parseHash(hash)).toEqual({ route, params, query: {} });
  });

  it('keeps the query of a legacy hash', () => {
    for (const legacy of ['run', 'batch', 'terminal']) {
      expect(parseHash(`#/${legacy}?target=a1&group=g`)).toEqual({
        route: 'fleet',
        params: ['commands'],
        query: { target: 'a1', group: 'g' },
      });
    }
    expect(parseHash('#/nodes?status=offline&group=arm')).toEqual({
      route: 'fleet',
      params: ['nodes'],
      query: { status: 'offline', group: 'arm' },
    });
    expect(parseHash('#/nodes/a1?x=1').query).toEqual({ x: '1' });
  });

  it('parses the wiki view of the Debates page', () => {
    expect(parseHash('#/debates/wiki')).toEqual({ route: 'debates', params: ['wiki'], query: {} });
    expect(parseHash('#/debates/wiki/cli-llm-agent')).toEqual({ route: 'debates', params: ['wiki', 'cli-llm-agent'], query: {} });
    expect(hrefFor('debates', ['wiki', 'cli-llm-agent'])).toBe('#/debates/wiki/cli-llm-agent');
  });

  it('turns the old monitor link #/agents?run=<id> into #/debates/<id>', () => {
    expect(parseHash('#/agents?run=j1')).toEqual({ route: 'debates', params: ['j1'], query: {} });
    expect(parseHash('#/debates?run=j2&x=1')).toEqual({ route: 'debates', params: ['j2'], query: { x: '1' } });
    // An explicit path wins over a stale ?run=.
    expect(parseHash('#/agents/j3?run=j1').params).toEqual(['j3']);
  });
});

describe('hrefFor', () => {
  it('builds hashes with params and query', () => {
    expect(hrefFor('fleet')).toBe('#/fleet');
    expect(hrefFor('fleet', ['nodes', 'a 1'])).toBe('#/fleet/nodes/a%201');
    expect(hrefFor('fleet', ['commands'], { target: 'a1' })).toBe('#/fleet/commands?target=a1');
    expect(parseHash(hrefFor('debates', ['j/1'], { q: 'a b' }))).toEqual({ route: 'debates', params: ['j/1'], query: { q: 'a b' } });
  });

  it('builds Fleet tab hashes; the summary is the bare #/fleet', () => {
    expect(fleetHref('summary')).toBe('#/fleet');
    expect(fleetHref('nodes')).toBe('#/fleet/nodes');
    expect(fleetHref('nodes', ['a1'])).toBe('#/fleet/nodes/a1');
    expect(fleetHref('ssh')).toBe('#/fleet/ssh');
    expect(fleetHref('commands', [], { target: 'group:lab' })).toBe('#/fleet/commands?target=group%3Alab');
  });

  it('reads the Fleet tab from the first param; unknown values show the summary', () => {
    expect(fleetTab(undefined)).toBe('summary');
    expect(fleetTab('nodes')).toBe('nodes');
    expect(fleetTab('ssh')).toBe('ssh');
    expect(fleetTab('commands')).toBe('commands');
    expect(fleetTab('bogus')).toBe('summary');
  });
});

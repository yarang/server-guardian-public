import { describe, expect, it } from 'vitest';
import { ROUTES, fleetHref, fleetTab, hrefFor, parseHash } from '../src/router';

describe('parseHash', () => {
  it('parses route, params and query', () => {
    expect(parseHash('#/fleet/nodes/a1?x=1&y=two')).toEqual({ route: 'fleet', params: ['nodes', 'a1'], query: { x: '1', y: 'two' } });
    expect(parseHash('#debates/job%2F7/extra')).toEqual({ route: 'debates', params: ['job/7', 'extra'], query: {} });
    expect(parseHash('#/fleet/commands?target=a1')).toEqual({ route: 'fleet', params: ['commands'], query: { target: 'a1' } });
    expect(parseHash('#/approvals/9')).toEqual({ route: 'approvals', params: ['9'], query: {} });
    expect(parseHash('#/overview')).toEqual({ route: 'overview', params: [], query: {} });
    // Node groups and command sets moved from Settings to Fleet tabs; the old deep links keep working.
    expect(parseHash('#/settings/groups?edit=lab')).toEqual({ route: 'fleet', params: ['groups'], query: { edit: 'lab' } });
    expect(parseHash('#/settings/commandSets')).toEqual({ route: 'fleet', params: ['commandSets'], query: {} });
    expect(parseHash('#/fleet/groups')).toEqual({ route: 'fleet', params: ['groups'], query: {} });
    expect(parseHash('#/fleet/commandSets')).toEqual({ route: 'fleet', params: ['commandSets'], query: {} });
    expect(parseHash('#/settings/teams?edit=x')).toEqual({ route: 'settings', params: ['teams'], query: { edit: 'x' } });
  });

  it('defaults to Overview for empty and unknown hashes', () => {
    expect(parseHash('')).toEqual({ route: 'overview', params: [], query: {} });
    expect(parseHash('#/')).toMatchObject({ route: 'overview' });
    expect(parseHash('#/nope/x?y=1')).toEqual({ route: 'overview', params: [], query: {} });
  });

  it('has exactly five destinations', () => {
    expect(ROUTES.map((r) => r.id)).toEqual(['overview', 'fleet', 'debates', 'approvals', 'settings']);
  });

  it.each([
    ['#/fleet', 'overview', []],
    ['#/fleet/summary', 'overview', []],
    ['#/overview', 'overview', []],
    ['#/assistant', 'overview', []],
    ['#/more', 'overview', []],
    ['#/nodes', 'fleet', ['nodes']],
    ['#/nodes/a1', 'fleet', ['nodes', 'a1']],
    ['#/nodes/new', 'fleet', ['nodes', 'new']],
    ['#/security', 'fleet', ['ssh']],
    ['#/ssh', 'fleet', ['ssh']],
    ['#/run', 'fleet', ['commands']],
    ['#/batch', 'fleet', ['commands']],
    ['#/terminal', 'fleet', ['commands']],
    ['#/settings/commandSets', 'fleet', ['commandSets']],
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
    // The old summary hashes keep their query when they land on Overview.
    for (const legacy of ['fleet', 'fleet/summary']) {
      expect(parseHash(`#/${legacy}?x=1`)).toEqual({ route: 'overview', params: [], query: { x: '1' } });
    }
    expect(parseHash('#/settings/commandSets?edit=x')).toEqual({ route: 'fleet', params: ['commandSets'], query: { edit: 'x' } });
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
    expect(hrefFor('overview')).toBe('#/overview');
    expect(hrefFor('fleet')).toBe('#/fleet');
    expect(hrefFor('fleet', ['nodes', 'a 1'])).toBe('#/fleet/nodes/a%201');
    expect(hrefFor('fleet', ['commands'], { target: 'a1' })).toBe('#/fleet/commands?target=a1');
    expect(parseHash(hrefFor('debates', ['j/1'], { q: 'a b' }))).toEqual({ route: 'debates', params: ['j/1'], query: { q: 'a b' } });
  });

  it('builds Fleet tab hashes; every tab has its own path segment', () => {
    expect(fleetHref('nodes')).toBe('#/fleet/nodes');
    expect(fleetHref('nodes', ['a1'])).toBe('#/fleet/nodes/a1');
    expect(fleetHref('groups')).toBe('#/fleet/groups');
    expect(fleetHref('ssh')).toBe('#/fleet/ssh');
    expect(fleetHref('commands', [], { target: 'group:lab' })).toBe('#/fleet/commands?target=group%3Alab');
    expect(fleetHref('commandSets')).toBe('#/fleet/commandSets');
  });

  it('reads the Fleet tab from the first param; unknown values show the node list', () => {
    expect(fleetTab(undefined)).toBe('nodes');
    expect(fleetTab('nodes')).toBe('nodes');
    expect(fleetTab('groups')).toBe('groups');
    expect(fleetTab('ssh')).toBe('ssh');
    expect(fleetTab('commands')).toBe('commands');
    expect(fleetTab('commandSets')).toBe('commandSets');
    expect(fleetTab('bogus')).toBe('nodes');
  });
});

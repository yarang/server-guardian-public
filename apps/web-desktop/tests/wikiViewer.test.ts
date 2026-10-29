import { describe, expect, it } from 'vitest';
import { attentionPages, buildWikiTree, filterPages, groupParentId, indexOrder, pageNeighbours, parentOf, referencedBy } from '../src/lib/wiki';
import type { WikiPageSummary } from '../src/types';

const llm = { kind: 'llm', provider: 'claude', job: 'wiki_1' };
const page = (id: string, extra: Partial<WikiPageSummary> = {}): WikiPageSummary => ({
  id, title: id.toUpperCase(), scope: [`${id}/`], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh', ...extra,
});

// Storage order, as the hub sends it: ids sorted, so a group sits next to its overview but features are scattered.
const list: WikiPageSummary[] = [
  page('apps-agent'),
  page('cli', { kind: 'overview' }),
  page('cli--engine', { kind: 'group' }),
  page('cli--wiki', { kind: 'group', state: 'stale' }),
  page('feature-auth', { kind: 'feature' }),
  page('scripts', { state: 'broken', links: ['cli'] }),
];

describe('wiki viewer helpers', () => {
  it('puts groups under their overview and keeps features apart', () => {
    const tree = buildWikiTree(list);
    expect(tree.roots.map((n) => [n.page.id, n.groups.map((g) => g.id)])).toEqual([
      ['apps-agent', []],
      ['cli', ['cli--engine', 'cli--wiki']],
      ['scripts', []],
    ]);
    expect(tree.features.map((f) => f.id)).toEqual(['feature-auth']);
  });

  it('gives a reading order that is a permutation of the list', () => {
    const order = indexOrder(buildWikiTree(list));
    expect(order.map((p) => p.id)).toEqual(['apps-agent', 'cli', 'cli--engine', 'cli--wiki', 'scripts', 'feature-auth']);
    expect([...order.map((p) => p.id)].sort()).toEqual(list.map((p) => p.id).sort());
  });

  it('treats a missing kind as dir and never derives a parent for a dir id that contains --', () => {
    const odd = [page('a--b'), page('a')]; // both plain dir pages
    const tree = buildWikiTree(odd);
    expect(tree.roots.map((n) => n.page.id)).toEqual(['a--b', 'a']);
    expect(groupParentId(odd[0], new Set(['a', 'a--b']))).toBeNull();
  });

  it('shows a group whose overview is missing, or is not a root, as a root of its own', () => {
    const orphan = buildWikiTree([page('x--y', { kind: 'group' })]);
    expect(orphan.roots.map((n) => n.page.id)).toEqual(['x--y']);
    const underFeature = buildWikiTree([page('feature-x', { kind: 'feature' }), page('feature-x--part', { kind: 'group' })]);
    expect(underFeature.roots.map((n) => n.page.id)).toEqual(['feature-x--part']);
    expect(underFeature.features.map((f) => f.id)).toEqual(['feature-x']);
  });

  it('picks the longest known prefix as the parent', () => {
    const known = new Set(['a', 'a--b']);
    expect(groupParentId({ id: 'a--b--c', kind: 'group' }, known)).toBe('a--b');
    expect(groupParentId({ id: 'a--z', kind: 'group' }, known)).toBe('a');
    expect(groupParentId({ id: 'q--z', kind: 'group' }, known)).toBeNull();
  });

  it('finds the previous and next page and the parent', () => {
    const tree = buildWikiTree(list);
    const order = indexOrder(tree);
    expect(pageNeighbours(order, 'cli')).toMatchObject({ prev: { id: 'apps-agent' }, next: { id: 'cli--engine' } });
    expect(pageNeighbours(order, 'apps-agent').prev).toBeNull();
    expect(pageNeighbours(order, 'feature-auth').next).toBeNull();
    expect(pageNeighbours(order, 'gone')).toEqual({ prev: null, next: null });
    expect(parentOf(tree, 'cli--wiki')?.id).toBe('cli');
    expect(parentOf(tree, 'cli')).toBeNull();
  });

  it('filters by title, id and scope without case, and lists the pages to distrust', () => {
    expect(filterPages(list, '  ').length).toBe(list.length);
    expect(filterPages(list, 'ENGINE').map((p) => p.id)).toEqual(['cli--engine']);
    expect(filterPages(list, 'feature-auth/').map((p) => p.id)).toEqual(['feature-auth']);
    expect(filterPages(list, 'nothing')).toEqual([]);
    expect(attentionPages(list).map((p) => p.id)).toEqual(['cli--wiki', 'scripts']);
  });

  it('lists the pages that link to a page, and none when the hub sends no links', () => {
    expect(referencedBy(list, 'cli').map((p) => p.id)).toEqual(['scripts']);
    expect(referencedBy(list, 'scripts')).toEqual([]);
    expect(referencedBy(list.map((p) => ({ ...p, links: undefined })), 'cli')).toEqual([]);
  });
});
